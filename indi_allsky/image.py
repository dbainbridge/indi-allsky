import io
import json
from pathlib import Path
from datetime import datetime
from datetime import timedelta
#from datetime import timezone
import time
import functools
import tempfile
import shutil
import copy
import math
import logging
import traceback
#from pprint import pformat

import ephem

from multiprocessing import Process
#from threading import Thread
import queue

from astropy.io import fits
import astroalign

import cv2
import numpy

from .orb import IndiAllskyOrbGenerator
from .sqm import IndiAllskySqm
from .stars import IndiAllSkyStars
from .detectLines import IndiAllskyDetectLines
from .draw import IndiAllSkyDraw
from .scnr import IndiAllskyScnr

from .flask import db
from .flask.miscDb import miscDb

from .flask.models import TaskQueueState
from .flask.models import TaskQueueQueue
from .flask.models import IndiAllSkyDbCameraTable
from .flask.models import IndiAllSkyDbImageTable
from .flask.models import IndiAllSkyDbBadPixelMapTable
from .flask.models import IndiAllSkyDbDarkFrameTable
from .flask.models import IndiAllSkyDbTaskQueueTable

from sqlalchemy import func
#from sqlalchemy.orm.exc import NoResultFound

from .exceptions import CalibrationNotFound


try:
    import rawpy  # not available in all cases
except ImportError:
    rawpy = None


logger = logging.getLogger('indi_allsky')



class ImageWorker(Process):

    sqm_history_minutes = 30
    stars_history_minutes = 30

    def __init__(
        self,
        idx,
        config,
        error_q,
        image_q,
        upload_q,
        latitude_v,
        longitude_v,
        ra_v,
        dec_v,
        exposure_v,
        gain_v,
        bin_v,
        sensortemp_v,
        night_v,
        moonmode_v,
    ):
        super(ImageWorker, self).__init__()

        #self.threadID = idx
        self.name = 'ImageWorker{0:03d}'.format(idx)

        self.config = config
        self.error_q = error_q
        self.image_q = image_q
        self.upload_q = upload_q

        self.latitude_v = latitude_v
        self.longitude_v = longitude_v

        self.ra_v = ra_v
        self.dec_v = dec_v

        self.exposure_v = exposure_v
        self.gain_v = gain_v
        self.bin_v = bin_v
        self.sensortemp_v = sensortemp_v
        self.night_v = night_v
        self.moonmode_v = moonmode_v

        # shared between objects
        self.astrometric_data = {
            'sun_alt'       : 0.0,
            'moon_alt'      : 0.0,
            'moon_phase'    : 0.0,
            'sun_moon_sep'  : 90.0,
            'sidereal_time' : 'unset',
        }

        self.filename_t = 'ccd{0:d}_{1:s}.{2:s}'

        self.target_adu_found = False
        self.current_adu_target = 0
        self.hist_adu = []
        self.target_adu = float(self.config['TARGET_ADU'])

        self.image_count = 0

        self.sqm_value = 0

        self._detection_mask = self._load_detection_mask()
        self._adu_mask = self._detection_mask  # reuse detection mask for ADU mask (if defined)


        self.image_processor = ImageProcessor(self.config, latitude_v, longitude_v, ra_v, dec_v, exposure_v, gain_v, bin_v, sensortemp_v, night_v, moonmode_v, self.astrometric_data, mask=self._detection_mask)


        self._miscDb = miscDb(self.config)

        if self.config.get('IMAGE_FOLDER'):
            self.image_dir = Path(self.config['IMAGE_FOLDER']).absolute()
        else:
            self.image_dir = Path(__file__).parent.parent.joinpath('html', 'images').absolute()



    def run(self):
        ### use this as a method to log uncaught exceptions
        try:
            self.saferun()
        except Exception as e:
            tb = traceback.format_exc()
            self.error_q.put((str(e), tb))
            raise e



    def saferun(self):
        #raise Exception('Test exception handling in worker')

        while True:
            try:
                i_dict = self.image_q.get(timeout=23)  # prime number
            except queue.Empty:
                continue

            if i_dict.get('stop'):
                return

            ### Not using DB task queue for image processing to reduce database I/O
            #task_id = i_dict['task_id']

            #try:
            #    task = IndiAllSkyDbTaskQueueTable.query\
            #        .filter(IndiAllSkyDbTaskQueueTable.id == task_id)\
            #        .filter(IndiAllSkyDbTaskQueueTable.state == TaskQueueState.QUEUED)\
            #        .filter(IndiAllSkyDbTaskQueueTable.queue == TaskQueueQueue.IMAGE)\
            #        .one()

            #except NoResultFound:
            #    logger.error('Task ID %d not found', task_id)
            #    continue


            #task.setRunning()


            #filename = Path(task.data['filename'])
            #exposure = task.data['exposure']
            #exp_date = datetime.fromtimestamp(task.data['exp_time'])
            #exp_elapsed = task.data['exp_elapsed']
            #camera_id = task.data['camera_id']
            #filename_t = task.data.get('filename_t')
            ###

            filename_p = Path(i_dict['filename'])
            exposure = i_dict['exposure']
            exp_date = datetime.fromtimestamp(i_dict['exp_time'])
            exp_elapsed = i_dict['exp_elapsed']
            camera_id = i_dict['camera_id']
            filename_t = i_dict.get('filename_t')


            if filename_t:
                self.filename_t = filename_t

            self.image_count += 1


            if not filename_p.exists():
                logger.error('Frame not found: %s', filename_p)
                #task.setFailed('Frame not found: {0:s}'.format(str(filename_p)))
                continue


            if filename_p.stat().st_size == 0:
                logger.error('Frame is empty: %s', filename_p)
                filename_p.unlink()
                continue


            processing_start = time.time()


            self.image_processor.add(filename_p, exposure, exp_date, exp_elapsed, camera_id)
            self.image_processor.calibrate()


            if self.config.get('IMAGE_SAVE_FITS'):
                i_ref = self.image_processor.getLatestImage()
                self.write_fit(i_ref)


            self.image_processor.calculateSqm()

            self.image_processor.stack()

            self.image_processor.debayer()



            image_height, image_width = self.image_processor.image.shape[:2]
            logger.info('Image: %d x %d', image_width, image_height)


            i_ref = self.image_processor.getLatestImage()

            ### IMAGE IS CALIBRATED ###


            if self.config.get('IMAGE_EXPORT_RAW'):
                self.export_raw_image(i_ref)


            self.image_processor.convert_16bit_to_8bit()


            #with io.open('/tmp/indi_allsky_numpy.npy', 'w+b') as f_numpy:
            #    numpy.save(f_numpy, self.image_processor.image)
            #logger.info('Wrote Numpy data: /tmp/indi_allsky_numpy.npy')


            # rotation
            if self.config.get('IMAGE_ROTATE'):
                try:
                    rotate_enum = getattr(cv2, self.config['IMAGE_ROTATE'])
                    self.image_processor.rotate(rotate_enum)
                except AttributeError:
                    logger.error('Unknown rotation option: %s', self.config['IMAGE_ROTATE'])


            # verticle flip
            if self.config.get('IMAGE_FLIP_V'):
                self.image_processor.flip(0)

            # horizontal flip
            if self.config.get('IMAGE_FLIP_H'):
                self.image_processor.flip(1)


            # adu calculate (before processing)
            adu, adu_average = self.calculate_histogram(self.image_processor.image, exposure)


            # line detection
            if self.night_v.value and self.config.get('DETECT_METEORS'):
                self.image_processor.detectLines()


            # star detection
            if self.night_v.value and self.config.get('DETECT_STARS', True):
                self.image_processor.detectStars()


            # additional draw code
            if self.config.get('DETECT_DRAW'):
                self.image_processor.drawDetections()


            # crop
            if self.config.get('IMAGE_CROP_ROI'):
                self.image_processor.crop_image()


            # green removal
            scnr_algo = self.config.get('SCNR_ALGORITHM')
            if scnr_algo:
                self.image_processor.scnr(scnr_algo)


            # white balance
            self.image_processor.white_balance_manual_bgr()

            if self.config.get('AUTO_WB'):
                self.image_processor.white_balance_auto_bgr()


            if not self.night_v.value and self.config['DAYTIME_CONTRAST_ENHANCE']:
                # Contrast enhancement during the day
                self.image_processor.contrast_clahe()
            elif self.night_v.value and self.config['NIGHT_CONTRAST_ENHANCE']:
                # Contrast enhancement during night
                self.image_processor.contrast_clahe()


            if self.config['IMAGE_SCALE'] and self.config['IMAGE_SCALE'] != 100:
                self.image_processor.scale_image()


            # blur
            #self.image_processor.median_blur()

            # denoise
            #self.image_processor.fastDenoise()

            self.image_processor.image_text()


            processing_elapsed_s = time.time() - processing_start
            logger.info('Image processed in %0.4f s', processing_elapsed_s)


            #task.setSuccess('Image processed')

            self.write_status_json(i_ref, adu, adu_average)  # write json status file

            latest_file, new_filename = self.write_img(self.image_processor.image, i_ref)

            if new_filename:
                image_entry = self._miscDb.addImage(
                    new_filename,
                    camera_id,
                    exp_date,
                    exposure,
                    exp_elapsed,
                    self.gain_v.value,
                    self.bin_v.value,
                    self.sensortemp_v.value,
                    adu,
                    self.target_adu_found,  # stable
                    bool(self.moonmode_v.value),
                    self.astrometric_data['moon_phase'],
                    night=bool(self.night_v.value),
                    adu_roi=self.config['ADU_ROI'],
                    calibrated=i_ref['calibrated'],
                    sqm=i_ref['sqm_value'],
                    stars=len(i_ref['stars']),
                    detections=len(i_ref['lines']),
                )
            else:
                # images not being saved
                image_entry = None


            if latest_file:
                # build mqtt data
                mqtt_data = {
                    'exposure' : round(exposure, 6),
                    'gain'     : self.gain_v.value,
                    'bin'      : self.bin_v.value,
                    'temp'     : round(self.sensortemp_v.value, 1),
                    'sunalt'   : round(self.astrometric_data['sun_alt'], 1),
                    'moonalt'  : round(self.astrometric_data['moon_alt'], 1),
                    'moonphase': round(self.astrometric_data['moon_phase'], 1),
                    'moonmode' : bool(self.moonmode_v.value),
                    'night'    : bool(self.night_v.value),
                    'sqm'      : round(i_ref['sqm_value'], 1),
                    'stars'    : len(i_ref['stars']),
                    'latitude' : round(self.latitude_v.value, 3),
                    'longitude': round(self.longitude_v.value, 3),
                    'sidereal_time': self.astrometric_data['sidereal_time'],
                }

                self.mqtt_publish(latest_file, mqtt_data)


                self.upload_image(i_ref, latest_file, image_entry=image_entry)
                self.upload_metadata(i_ref, adu, adu_average)


    def upload_image(self, i_ref, latest_file, image_entry=None):
        ### upload images
        if not self.config.get('FILETRANSFER', {}).get('UPLOAD_IMAGE'):
            #logger.warning('Image uploading disabled')
            return

        if (self.image_count % int(self.config['FILETRANSFER']['UPLOAD_IMAGE'])) != 0:
            next_image = int(self.config['FILETRANSFER']['UPLOAD_IMAGE']) - (self.image_count % int(self.config['FILETRANSFER']['UPLOAD_IMAGE']))
            logger.info('Next image upload in %d images (%d s)', next_image, int(self.config['EXPOSURE_PERIOD'] * next_image))
            return


        # Parameters for string formatting
        file_data_list = [
            self.config['IMAGE_FILE_TYPE'],
        ]

        file_data_dict = {
            'timestamp'    : i_ref['exp_date'],
            'ts'           : i_ref['exp_date'],  # shortcut
            'ext'          : self.config['IMAGE_FILE_TYPE'],
        }


        # Replace parameters in names
        remote_dir = self.config['FILETRANSFER']['REMOTE_IMAGE_FOLDER'].format(**file_data_dict)
        remote_file = self.config['FILETRANSFER']['REMOTE_IMAGE_NAME'].format(*file_data_list, **file_data_dict)


        remote_file_p = Path(remote_dir).joinpath(remote_file)


        # tell worker to upload file
        jobdata = {
            'action'      : 'upload',
            'local_file'  : str(latest_file),
            'remote_file' : str(remote_file_p),
        }

        upload_task = IndiAllSkyDbTaskQueueTable(
            queue=TaskQueueQueue.UPLOAD,
            state=TaskQueueState.QUEUED,
            data=jobdata,
        )
        db.session.add(upload_task)
        db.session.commit()

        self.upload_q.put({'task_id' : upload_task.id})

        if image_entry:
            # image was not saved
            self._miscDb.addUploadedFlag(image_entry)


    def upload_metadata(self, i_ref, adu, adu_average):
        ### upload images
        if not self.config.get('FILETRANSFER', {}).get('UPLOAD_METADATA'):
            #logger.warning('Metadata uploading disabled')
            return

        if not self.config.get('FILETRANSFER', {}).get('UPLOAD_IMAGE'):
            logger.warning('Metadata uploading disabled when image upload is disabled')
            return

        ### Only uploading metadata if image uploading is enabled
        if (self.image_count % int(self.config['FILETRANSFER']['UPLOAD_IMAGE'])) != 0:
            #next_image = int(self.config['FILETRANSFER']['UPLOAD_IMAGE']) - (self.image_count % int(self.config['FILETRANSFER']['UPLOAD_IMAGE']))
            #logger.info('Next image upload in %d images (%d s)', next_image, int(self.config['EXPOSURE_PERIOD'] * next_image))
            return


        metadata = {
            'device'              : self.config['CCD_NAME'],
            'night'               : self.night_v.value,
            'temp'                : self.sensortemp_v.value,
            'gain'                : self.gain_v.value,
            'exposure'            : i_ref['exposure'],
            'stable_exposure'     : int(self.target_adu_found),
            'target_adu'          : self.target_adu,
            'current_adu_target'  : self.current_adu_target,
            'current_adu'         : adu,
            'adu_average'         : adu_average,
            'sqm'                 : i_ref['sqm_value'],
            'stars'               : len(i_ref['stars']),
            'time'                : i_ref['exp_date'].strftime('%s'),
            'sqm_data'            : self.getSqmData(i_ref['camera_id']),
            'stars_data'          : self.getStarsData(i_ref['camera_id']),
            'latitude'            : self.latitude_v.value,
            'longitude'           : self.longitude_v.value,
            'sidereal_time'       : self.astrometric_data['sidereal_time'],
        }


        f_tmp_metadata = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json')

        json.dump(metadata, f_tmp_metadata, indent=4)

        f_tmp_metadata.flush()
        f_tmp_metadata.close()

        tmp_metadata_name_p = Path(f_tmp_metadata.name)
        tmp_metadata_name_p.chmod(0o644)


        file_data_dict = {
            'timestamp'    : i_ref['exp_date'],
            'ts'           : i_ref['exp_date'],  # shortcut
        }

        # Replace parameters in names
        remote_dir = self.config['FILETRANSFER']['REMOTE_METADATA_FOLDER'].format(**file_data_dict)
        remote_file = self.config['FILETRANSFER']['REMOTE_METADATA_NAME'].format(**file_data_dict)

        remote_file_p = Path(remote_dir).joinpath(remote_file)

        # tell worker to upload file
        jobdata = {
            'action'       : 'upload',
            'local_file'   : str(tmp_metadata_name_p),
            'remote_file'  : str(remote_file_p),
            'remove_local' : True,
        }

        upload_task = IndiAllSkyDbTaskQueueTable(
            queue=TaskQueueQueue.UPLOAD,
            state=TaskQueueState.QUEUED,
            data=jobdata,
        )
        db.session.add(upload_task)
        db.session.commit()

        self.upload_q.put({'task_id' : upload_task.id})


    def mqtt_publish(self, latest_file, mq_data):
        if not self.config.get('MQTTPUBLISH', {}).get('ENABLE'):
            #logger.warning('MQ publishing disabled')
            return

        logger.info('Publishing data to MQ broker')

        # publish data to mq broker
        jobdata = {
            'action'      : 'mqttpub',
            'local_file'  : str(latest_file),
            'mq_data'     : mq_data,
        }

        mqtt_task = IndiAllSkyDbTaskQueueTable(
            queue=TaskQueueQueue.UPLOAD,
            state=TaskQueueState.QUEUED,
            data=jobdata,
        )
        db.session.add(mqtt_task)
        db.session.commit()

        self.upload_q.put({'task_id' : mqtt_task.id})


    def getSqmData(self, camera_id):
        now_minus_minutes = datetime.now() - timedelta(minutes=self.sqm_history_minutes)

        #createDate_local = func.datetime(IndiAllSkyDbImageTable.createDate, 'localtime', type_=DateTime).label('createDate_local')
        sqm_images = IndiAllSkyDbImageTable.query\
            .add_columns(
                func.max(IndiAllSkyDbImageTable.sqm).label('image_max_sqm'),
                func.min(IndiAllSkyDbImageTable.sqm).label('image_min_sqm'),
                func.avg(IndiAllSkyDbImageTable.sqm).label('image_avg_sqm'),
            )\
            .join(IndiAllSkyDbCameraTable)\
            .filter(IndiAllSkyDbCameraTable.id == camera_id)\
            .filter(IndiAllSkyDbImageTable.createDate > now_minus_minutes)\
            .first()


        sqm_data = {
            'max' : sqm_images.image_max_sqm,
            'min' : sqm_images.image_min_sqm,
            'avg' : sqm_images.image_avg_sqm,
        }

        return sqm_data


    def getStarsData(self, camera_id):
        now_minus_minutes = datetime.now() - timedelta(minutes=self.stars_history_minutes)

        #createDate_local = func.datetime(IndiAllSkyDbImageTable.createDate, 'localtime', type_=DateTime).label('createDate_local')
        stars_images = IndiAllSkyDbImageTable.query\
            .add_columns(
                func.max(IndiAllSkyDbImageTable.stars).label('image_max_stars'),
                func.min(IndiAllSkyDbImageTable.stars).label('image_min_stars'),
                func.avg(IndiAllSkyDbImageTable.stars).label('image_avg_stars'),
            )\
            .join(IndiAllSkyDbCameraTable)\
            .filter(IndiAllSkyDbCameraTable.id == camera_id)\
            .filter(IndiAllSkyDbImageTable.createDate > now_minus_minutes)\
            .first()


        stars_data = {
            'max' : stars_images.image_max_stars,
            'min' : stars_images.image_min_stars,
            'avg' : stars_images.image_avg_stars,
        }

        return stars_data


    def write_fit(self, i_ref):
        f_tmpfile = tempfile.NamedTemporaryFile(mode='w+b', delete=False, suffix='.fit')

        i_ref['hdulist'].writeto(f_tmpfile)

        f_tmpfile.flush()
        f_tmpfile.close()


        date_str = i_ref['exp_date'].strftime('%Y%m%d_%H%M%S')
        # raw light
        folder = self.getImageFolder(i_ref['exp_date'])
        filename = folder.joinpath(self.filename_t.format(
            i_ref['camera_id'],
            date_str,
            'fit',
        ))


        self._miscDb.addFitsImage(
            filename,
            i_ref['camera_id'],
            i_ref['exp_date'],
            i_ref['exposure'],
            self.gain_v.value,
            self.bin_v.value,
            night=bool(self.night_v.value),
        )


        file_dir = filename.parent
        if not file_dir.exists():
            file_dir.mkdir(mode=0o755, parents=True)

        logger.info('fit filename: %s', filename)


        if filename.exists():
            logger.error('File exists: %s (skipping)', filename)
            return

        shutil.copy2(f_tmpfile.name, str(filename))  # copy file in place
        filename.chmod(0o644)

        Path(f_tmpfile.name).unlink()  # delete temp file

        logger.info('Finished writing fit file')


    def export_raw_image(self, i_ref):
        if not self.config.get('IMAGE_EXPORT_RAW'):
            return

        if not self.config.get('IMAGE_EXPORT_FOLDER'):
            logger.error('IMAGE_EXPORT_FOLDER not defined')
            return

        data = i_ref['hdulist'][0].data

        if i_ref['image_bitpix'] == 8:
            # nothing to scale
            scaled_data = data
        elif i_ref['image_bitpix'] == 16:
            if i_ref['image_bit_depth'] == 8:
                logger.info('Upscaling data from 8 to 16 bit')
                scaled_data = numpy.left_shift(data, 8)
            elif i_ref['image_bit_depth'] == 9:
                logger.info('Upscaling data from 9 to 16 bit')
                scaled_data = numpy.left_shift(data, 7)
            elif i_ref['image_bit_depth'] == 10:
                logger.info('Upscaling data from 10 to 16 bit')
                scaled_data = numpy.left_shift(data, 6)
            elif i_ref['image_bit_depth'] == 11:
                logger.info('Upscaling data from 11 to 16 bit')
                scaled_data = numpy.left_shift(data, 5)
            elif i_ref['image_bit_depth'] == 12:
                logger.info('Upscaling data from 12 to 16 bit')
                scaled_data = numpy.left_shift(data, 4)
            elif i_ref['image_bit_depth'] == 13:
                logger.info('Upscaling data from 13 to 16 bit')
                scaled_data = numpy.left_shift(data, 3)
            elif i_ref['image_bit_depth'] == 14:
                logger.info('Upscaling data from 14 to 16 bit')
                scaled_data = numpy.left_shift(data, 2)
            elif i_ref['image_bit_depth'] == 15:
                logger.info('Upscaling data from 15 to 16 bit')
                scaled_data = numpy.left_shift(data, 1)
            elif i_ref['image_bit_depth'] == 16:
                # nothing to scale
                scaled_data = data
            else:
                # assume 16 bit
                scaled_data = data
        else:
            raise Exception('Unsupported bit depth')


        export_dir = Path(self.config['IMAGE_EXPORT_FOLDER'])

        if self.night_v.value:
            # images should be written to previous day's folder until noon
            day_ref = i_ref['exp_date'] - timedelta(hours=12)
            timeofday_str = 'night'
        else:
            # daytime
            # images should be written to current day's folder
            day_ref = i_ref['exp_date']
            timeofday_str = 'day'

        date_str = i_ref['exp_date'].strftime('%Y%m%d_%H%M%S')

        hour_str = i_ref['exp_date'].strftime('%d_%H')

        day_folder = export_dir.joinpath('{0:s}'.format(day_ref.strftime('%Y%m%d')), timeofday_str)
        if not day_folder.exists():
            day_folder.mkdir(mode=0o755, parents=True)

        hour_folder = day_folder.joinpath('{0:s}'.format(hour_str))
        if not hour_folder.exists():
            hour_folder.mkdir(mode=0o755)


        raw_filename_t = 'raw_{0:s}'.format(self.filename_t)
        filename = hour_folder.joinpath(raw_filename_t.format(
            i_ref['camera_id'],
            date_str,
            self.config['IMAGE_EXPORT_RAW'],  # file suffix
        ))


        self._miscDb.addRawImage(
            filename,
            i_ref['camera_id'],
            i_ref['exp_date'],
            i_ref['exposure'],
            self.gain_v.value,
            self.bin_v.value,
            night=bool(self.night_v.value),
        )


        logger.info('RAW filename: %s', filename)

        write_img_start = time.time()

        if self.config['IMAGE_EXPORT_RAW'] in ('png',):
            cv2.imwrite(str(filename), scaled_data, [cv2.IMWRITE_PNG_COMPRESSION, self.config['IMAGE_FILE_COMPRESSION']['png']])
        elif self.config['IMAGE_EXPORT_RAW'] in ('tif', 'tiff'):
            cv2.imwrite(str(filename), scaled_data, [cv2.IMWRITE_TIFF_COMPRESSION, self.config['IMAGE_FILE_COMPRESSION']['tif']])
        else:
            raise Exception('Unknown file type: %s', self.config['IMAGE_EXPORT_RAW'])

        write_img_elapsed_s = time.time() - write_img_start
        logger.info('Raw image written in %0.4f s', write_img_elapsed_s)


    def write_img(self, data, i_ref):
        f_tmpfile = tempfile.NamedTemporaryFile(mode='w+b', delete=False, suffix='.{0}'.format(self.config['IMAGE_FILE_TYPE']))
        f_tmpfile.close()

        tmpfile_name = Path(f_tmpfile.name)
        tmpfile_name.unlink()  # remove tempfile, will be reused below


        write_img_start = time.time()

        # write to temporary file
        if self.config['IMAGE_FILE_TYPE'] in ('jpg', 'jpeg'):
            cv2.imwrite(str(tmpfile_name), data, [cv2.IMWRITE_JPEG_QUALITY, self.config['IMAGE_FILE_COMPRESSION']['jpg']])
        elif self.config['IMAGE_FILE_TYPE'] in ('png',):
            cv2.imwrite(str(tmpfile_name), data, [cv2.IMWRITE_PNG_COMPRESSION, self.config['IMAGE_FILE_COMPRESSION']['png']])
        elif self.config['IMAGE_FILE_TYPE'] in ('tif', 'tiff'):
            cv2.imwrite(str(tmpfile_name), data, [cv2.IMWRITE_TIFF_COMPRESSION, self.config['IMAGE_FILE_COMPRESSION']['tif']])
        else:
            raise Exception('Unknown file type: %s', self.config['IMAGE_FILE_TYPE'])

        write_img_elapsed_s = time.time() - write_img_start
        logger.info('Image compressed in %0.4f s', write_img_elapsed_s)


        ### Always write the latest file for web access
        latest_file = self.image_dir.joinpath('latest.{0:s}'.format(self.config['IMAGE_FILE_TYPE']))

        try:
            latest_file.unlink()
        except FileNotFoundError:
            pass

        shutil.copy2(str(tmpfile_name), str(latest_file))
        latest_file.chmod(0o644)


        ### disable timelapse images in focus mode
        if self.config.get('FOCUS_MODE', False):
            logger.warning('Focus mode enabled, not saving timelapse image')
            tmpfile_name.unlink()  # cleanup temp file
            return None, None


        ### Do not write daytime image files if daytime timelapse is disabled
        if not self.night_v.value and not self.config['DAYTIME_TIMELAPSE']:
            logger.info('Daytime timelapse is disabled')
            tmpfile_name.unlink()  # cleanup temp file
            return latest_file, None


        ### Write the timelapse file
        folder = self.getImageFolder(i_ref['exp_date'])

        date_str = i_ref['exp_date'].strftime('%Y%m%d_%H%M%S')
        filename = folder.joinpath(self.filename_t.format(i_ref['camera_id'], date_str, self.config['IMAGE_FILE_TYPE']))

        #logger.info('Image filename: %s', filename)

        if filename.exists():
            logger.error('File exists: %s (skipping)', filename)
            return latest_file, None

        shutil.copy2(str(tmpfile_name), str(filename))
        filename.chmod(0o644)


        ### Cleanup
        tmpfile_name.unlink()

        #logger.info('Finished writing files')

        return latest_file, filename


    def write_status_json(self, i_ref, adu, adu_average):
        status = {
            'name'                : 'indi_json',
            'class'               : 'ccd',
            'device'              : self.config['CCD_NAME'],
            'night'               : self.night_v.value,
            'temp'                : self.sensortemp_v.value,
            'gain'                : self.gain_v.value,
            'exposure'            : i_ref['exposure'],
            'stable_exposure'     : int(self.target_adu_found),
            'target_adu'          : self.target_adu,
            'current_adu_target'  : self.current_adu_target,
            'current_adu'         : adu,
            'adu_average'         : adu_average,
            'sqm'                 : i_ref['sqm_value'],
            'stars'               : len(i_ref['stars']),
            'time'                : i_ref['exp_date'].strftime('%s'),
            'latitude'            : self.latitude_v.value,
            'longitude'           : self.longitude_v.value,
        }


        indi_allsky_status_p = Path('/var/lib/indi-allsky/indi_allsky_status.json')

        with io.open(str(indi_allsky_status_p), 'w') as f_indi_status:
            json.dump(status, f_indi_status, indent=4)
            f_indi_status.flush()
            f_indi_status.close()

        indi_allsky_status_p.chmod(0o644)


    def getImageFolder(self, exp_date):
        if self.night_v.value:
            # images should be written to previous day's folder until noon
            day_ref = exp_date - timedelta(hours=12)
            timeofday_str = 'night'
        else:
            # daytime
            # images should be written to current day's folder
            day_ref = exp_date
            timeofday_str = 'day'

        hour_str = exp_date.strftime('%d_%H')

        day_folder = self.image_dir.joinpath('{0:s}'.format(day_ref.strftime('%Y%m%d')), timeofday_str)
        if not day_folder.exists():
            day_folder.mkdir(mode=0o755, parents=True)

        hour_folder = day_folder.joinpath('{0:s}'.format(hour_str))
        if not hour_folder.exists():
            hour_folder.mkdir(mode=0o755)

        return hour_folder


    def calculate_histogram(self, data, exposure):
        if isinstance(self._adu_mask, type(None)):
            # This only needs to be done once if a mask is not provided
            self._generateAduMask(data)


        if len(data.shape) == 2:
            # mono
            m_avg = cv2.mean(src=data, mask=self._adu_mask)[0]

            logger.info('Greyscale mean: %0.2f', m_avg)

            adu = m_avg
        else:
            data_mono = cv2.cvtColor(data, cv2.COLOR_BGR2GRAY)

            m_avg = cv2.mean(src=data_mono, mask=self._adu_mask)[0]

            logger.info('Greyscale mean: %0.2f', m_avg)

            adu = m_avg


        if adu <= 0.0:
            # ensure we do not divide by zero
            logger.warning('Zero average, setting a default of 0.1')
            adu = 0.1


        logger.info('Brightness average: %0.2f', adu)


        # Brightness when the sun is in view (very short exposures) can change drastically when clouds pass through the view
        # Setting a deviation that is too short can cause exposure flapping
        if exposure < 0.001000:
            # DAY
            adu_dev = float(self.config.get('TARGET_ADU_DEV_DAY', 20))

            target_adu_min = self.target_adu - adu_dev
            target_adu_max = self.target_adu + adu_dev
            current_adu_target_min = self.current_adu_target - adu_dev
            current_adu_target_max = self.current_adu_target + adu_dev

            exp_scale_factor = 0.50  # scale exposure calculation
            history_max_vals = 6     # number of entries to use to calculate average
        else:
            # NIGHT
            adu_dev = float(self.config.get('TARGET_ADU_DEV', 10))

            target_adu_min = self.target_adu - adu_dev
            target_adu_max = self.target_adu + adu_dev
            current_adu_target_min = self.current_adu_target - adu_dev
            current_adu_target_max = self.current_adu_target + adu_dev

            exp_scale_factor = 1.0  # scale exposure calculation
            history_max_vals = 6    # number of entries to use to calculate average



        if not self.target_adu_found:
            self.recalculate_exposure(exposure, adu, target_adu_min, target_adu_max, exp_scale_factor)
            return adu, 0.0


        self.hist_adu.append(adu)
        self.hist_adu = self.hist_adu[(history_max_vals * -1):]  # remove oldest values, up to history_max_vals

        logger.info('Current target ADU: %0.2f (%0.2f/%0.2f)', self.current_adu_target, current_adu_target_min, current_adu_target_max)
        logger.info('Current ADU history: (%d) [%s]', len(self.hist_adu), ', '.join(['{0:0.2f}'.format(x) for x in self.hist_adu]))


        adu_average = functools.reduce(lambda a, b: a + b, self.hist_adu) / len(self.hist_adu)
        logger.info('ADU average: %0.2f', adu_average)


        ### Need at least x values to continue
        if len(self.hist_adu) < history_max_vals:
            return adu, 0.0


        ### only change exposure when 70% of the values exceed the max or minimum
        if adu_average > current_adu_target_max:
            logger.warning('ADU increasing beyond limits, recalculating next exposure')
            self.target_adu_found = False
        elif adu_average < current_adu_target_min:
            logger.warning('ADU decreasing beyond limits, recalculating next exposure')
            self.target_adu_found = False

        return adu, adu_average


    def recalculate_exposure(self, exposure, adu, target_adu_min, target_adu_max, exp_scale_factor):

        # Until we reach a good starting point, do not calculate a moving average
        if adu <= target_adu_max and adu >= target_adu_min:
            logger.warning('Found target value for exposure')
            self.current_adu_target = copy.copy(adu)
            self.target_adu_found = True
            self.hist_adu = []
            return


        # Scale the exposure up and down based on targets
        if adu > target_adu_max:
            new_exposure = exposure - ((exposure - (exposure * (self.target_adu / adu))) * exp_scale_factor)
        elif adu < target_adu_min:
            new_exposure = exposure - ((exposure - (exposure * (self.target_adu / adu))) * exp_scale_factor)
        else:
            new_exposure = exposure



        # Do not exceed the limits
        if new_exposure < self.config['CCD_EXPOSURE_MIN']:
            new_exposure = self.config['CCD_EXPOSURE_MIN']
        elif new_exposure > self.config['CCD_EXPOSURE_MAX']:
            new_exposure = self.config['CCD_EXPOSURE_MAX']


        logger.warning('New calculated exposure: %0.6f', new_exposure)
        with self.exposure_v.get_lock():
            self.exposure_v.value = new_exposure


    def _generateAduMask(self, img):
        logger.info('Generating mask based on ADU_ROI')

        image_height, image_width = img.shape[:2]

        # create a black background
        mask = numpy.zeros((image_height, image_width), dtype=numpy.uint8)

        adu_roi = self.config.get('ADU_ROI', [])

        try:
            x1 = int(adu_roi[0] / self.bin_v.value)
            y1 = int(adu_roi[1] / self.bin_v.value)
            x2 = int(adu_roi[2] / self.bin_v.value)
            y2 = int(adu_roi[3] / self.bin_v.value)
        except IndexError:
            logger.warning('Using central ROI for ADU calculations')
            x1 = int((image_width / 2) - (image_width / 3))
            y1 = int((image_height / 2) - (image_height / 3))
            x2 = int((image_width / 2) + (image_width / 3))
            y2 = int((image_height / 2) + (image_height / 3))

        # The white area is what we keep
        cv2.rectangle(
            img=mask,
            pt1=(x1, y1),
            pt2=(x2, y2),
            color=(255),  # mono
            thickness=cv2.FILLED,
        )

        self._adu_mask = mask


    def _load_detection_mask(self):
        detect_mask = self.config.get('DETECT_MASK', '')

        if not detect_mask:
            logger.warning('No detection mask defined')
            return


        detect_mask_p = Path(detect_mask)

        try:
            if not detect_mask_p.exists():
                logger.error('%s does not exist', detect_mask_p)
                return


            if not detect_mask_p.is_file():
                logger.error('%s is not a file', detect_mask_p)
                return

        except PermissionError as e:
            logger.error(str(e))
            return

        mask_data = cv2.imread(str(detect_mask_p), cv2.IMREAD_GRAYSCALE)  # mono
        if isinstance(mask_data, type(None)):
            logger.error('%s is not a valid image', detect_mask_p)
            return


        ### any compression artifacts will be set to black
        #mask_data[mask_data < 255] = 0  # did not quite work


        return mask_data



class ImageProcessor(object):

    dark_temperature_range = 5.0  # dark must be within this range

    registration_exposure_thresh = 5.0

    __cfa_bgr_map = {
        'GRBG' : cv2.COLOR_BAYER_GB2BGR,
        'RGGB' : cv2.COLOR_BAYER_BG2BGR,
        'BGGR' : cv2.COLOR_BAYER_RG2BGR,
        'GBRG' : cv2.COLOR_BAYER_GR2BGR,  # untested
    }

    __cfa_gray_map = {
        'GRBG' : cv2.COLOR_BAYER_GB2GRAY,
        'RGGB' : cv2.COLOR_BAYER_BG2GRAY,
        'BGGR' : cv2.COLOR_BAYER_RG2GRAY,
        'GBRG' : cv2.COLOR_BAYER_GR2GRAY,
    }


    def __init__(
        self,
        config,
        latitude_v,
        longitude_v,
        ra_v,
        dec_v,
        exposure_v,
        gain_v,
        bin_v,
        sensortemp_v,
        night_v,
        moonmode_v,
        astrometric_data,
        mask=None,
    ):
        self.config = config

        self.latitude_v = latitude_v
        self.longitude_v = longitude_v

        self.ra_v = ra_v
        self.dec_v = dec_v

        self.exposure_v = exposure_v
        self.gain_v = gain_v
        self.bin_v = bin_v
        self.sensortemp_v = sensortemp_v
        self.night_v = night_v
        self.moonmode_v = moonmode_v

        self.astrometric_data = astrometric_data

        self._detection_mask = mask

        self.focus_mode = self.config.get('FOCUS_MODE', False)

        self.stack_method = self.config.get('IMAGE_STACK_METHOD', 'average')
        self.stack_count = self.config.get('IMAGE_STACK_COUNT', 1)

        # contains the current stacked image
        self._image = None

        # contains the raw image data, data will be newest to oldest
        self.image_list = [None]  # element will be removed on first image

        self._orb = IndiAllskyOrbGenerator(self.config)
        self._sqm = IndiAllskySqm(self.config, self.bin_v, mask=None)
        self._stars = IndiAllSkyStars(self.config, self.bin_v, mask=self._detection_mask)
        self._lineDetect = IndiAllskyDetectLines(self.config, self.bin_v, mask=self._detection_mask)
        self._draw = IndiAllSkyDraw(self.config, self.bin_v, mask=self._detection_mask)
        self._scnr = IndiAllskyScnr(self.config)



    @property
    def image(self):
        return self._image

    @image.setter
    def image(self, new_image):
        self._image = new_image


    @property
    def shape(self):
        return self.image_list[0]['hdulist'].data.shape

    @shape.setter
    def shape(self, *args):
        pass  # read only



    def add(self, filename, exposure, exp_date, exp_elapsed, camera_id):
        filename_p = Path(filename)


        # clear old data as soon as possible
        self.image = None  # clear current data
        if self.night_v.value:
            if len(self.image_list) == self.stack_count:
                self.image_list.pop()  # remove last element
        else:
            # daytime
            self.image_list.clear()  # daytime only has one image


        indi_rgb = True  # INDI returns array in the wrong order for cv2

        ### Open file
        if filename_p.suffix in ['.fit']:
            hdulist = fits.open(filename_p)

            #logger.info('Initial HDU Header = %s', pformat(hdulist[0].header))
            image_bitpix = hdulist[0].header['BITPIX']
            image_bayerpat = hdulist[0].header.get('BAYERPAT')

            data = hdulist[0].data
        elif filename_p.suffix in ['.jpg', '.jpeg']:
            indi_rgb = False

            data = cv2.imread(str(filename_p), cv2.IMREAD_UNCHANGED)

            image_bitpix = 8
            image_bayerpat = None
        elif filename_p.suffix in ['.png']:
            indi_rgb = False

            data = cv2.imread(str(filename_p), cv2.IMREAD_UNCHANGED)

            image_bitpix = 8
            image_bayerpat = None
        elif filename_p.suffix in ['.dng']:
            if not rawpy:
                filename_p.unlink()
                raise Exception('*** rawpy module not available ***')

            # DNG raw
            raw = rawpy.imread(str(filename_p))
            data = raw.raw_image

            # create a new fits container for DNG data
            hdu = fits.PrimaryHDU(data)
            hdulist = fits.HDUList([hdu])

            hdulist[0].header['EXTEND'] = True
            hdulist[0].header['IMAGETYP'] = 'Light Frame'
            hdulist[0].header['INSTRUME'] = 'libcamera'
            hdulist[0].header['FOCALLEN'] = 10  # smallest possible value
            hdulist[0].header['APTDIA'] = 10  # smallest possible value
            hdulist[0].header['EXPTIME'] = float(exposure)
            hdulist[0].header['XBINNING'] = 1
            hdulist[0].header['YBINNING'] = 1
            hdulist[0].header['GAIN'] = float(self.gain_v.value)
            hdulist[0].header['CCD-TEMP'] = self.sensortemp_v.value
            hdulist[0].header['BITPIX'] = 16
            hdulist[0].header['SITELAT'] = self.latitude_v.value
            hdulist[0].header['SITELONG'] = self.longitude_v.value
            hdulist[0].header['RA'] = self.ra_v.value
            hdulist[0].header['DEC'] = self.dec_v.value
            hdulist[0].header['DATE-OBS'] = exp_date.isoformat()


            if self.config['CFA_PATTERN']:
                hdulist[0].header['BAYERPAT'] = self.config['CFA_PATTERN']
                hdulist[0].header['XBAYROFF'] = 0
                hdulist[0].header['YBAYROFF'] = 0

            image_bitpix = hdulist[0].header['BITPIX']
            image_bayerpat = hdulist[0].header.get('BAYERPAT')


        # Override these

        hdulist[0].header['OBJECT'] = 'AllSky'
        hdulist[0].header['TELESCOP'] = 'indi-allsky'


        # Add headers from config
        fitsheaders = self.config.get('FITSHEADERS', [])
        for header in fitsheaders:
            try:
                k = str(header[0]).upper()
                v = str(header[1])
            except IndexError:
                logger.error('Invalid header information')
                continue

            if not k:
                # skipping empty values
                continue

            if not v:
                # skipping empty values
                continue

            hdulist[0].header[k] = v


        #logger.info('Final HDU Header = %s', pformat(hdulist[0].header))


        filename_p.unlink()  # no longer need the original file


        logger.info('Image bits: %d, cfa: %s', image_bitpix, str(image_bayerpat))


        if not len(hdulist[0].data.shape) == 2:
            # color data

            if indi_rgb:
                # INDI returns array in the wrong order for cv2
                hdulist[0].data = numpy.swapaxes(hdulist[0].data, 0, 2)
                hdulist[0].data = numpy.swapaxes(hdulist[0].data, 0, 1)
                #logger.info('Channels: %s', pformat(hdulist[0].data.shape))

                hdulist[0].data = cv2.cvtColor(hdulist[0].data, cv2.COLOR_RGB2BGR)
            else:
                # normal rgb data
                pass


        image_bit_depth = self._detectBitDepth(hdulist)


        image_data = {
            'hdulist'          : hdulist,
            'calibrated'       : False,
            'exposure'         : exposure,
            'exp_date'         : exp_date,
            'exp_elapsed'      : exp_elapsed,
            'camera_id'        : camera_id,
            'image_bitpix'     : image_bitpix,
            'image_bayerpat'   : image_bayerpat,
            'image_bit_depth'  : image_bit_depth,
            'indi_rgb'         : indi_rgb,
            'sqm_value'        : None,    # populated later
            'lines'            : list(),  # populated later
            'stars'            : list(),  # populated later
        }


        self.image_list.insert(0, image_data)  # new image is first in list


    def _detectBitDepth(self, hdulist):
        ### This will need some rework if cameras return signed int data
        max_val = numpy.amax(hdulist[0].data)
        logger.info('Image max value: %d', int(max_val))

        # This method of detecting bit depth can cause the 16->8 bit conversion
        # to stretch too much.  This most commonly happens with very low gains
        # during the day when there are no hot pixels.  This can result in a
        # trippy effect
        if max_val > 32768:
            image_bit_depth = 16
        elif max_val > 16384:
            image_bit_depth = 15
        elif max_val > 8192:
            image_bit_depth = 14
        elif max_val > 4096:
            image_bit_depth = 13
        elif max_val > 2096:
            image_bit_depth = 12
        elif max_val > 1024:
            image_bit_depth = 11
        elif max_val > 512:
            image_bit_depth = 10
        elif max_val > 256:
            image_bit_depth = 9
        else:
            image_bit_depth = 8

        logger.info('Detected bit depth: %d', image_bit_depth)

        return image_bit_depth


    def getLatestImage(self):
        return self.image_list[0]


    def calibrate(self):
        i_ref = self.getLatestImage()

        if i_ref['calibrated']:
            # already calibrated
            return

        try:
            calibrated_data = self._calibrate(i_ref['hdulist'][0].data, i_ref['exposure'], i_ref['camera_id'], i_ref['image_bitpix'])
            i_ref['hdulist'][0].data = calibrated_data

            i_ref['calibrated'] = True
        except CalibrationNotFound:
            pass


    def _calibrate(self, data, exposure, camera_id, image_bitpix):
        # pick a bad pixel map that is closest to the exposure and temperature
        logger.info('Searching for bad pixel map: gain %d, exposure >= %0.1f, temp >= %0.1fc', self.gain_v.value, exposure, self.sensortemp_v.value)
        bpm_entry = IndiAllSkyDbBadPixelMapTable.query\
            .filter(IndiAllSkyDbBadPixelMapTable.camera_id == camera_id)\
            .filter(IndiAllSkyDbBadPixelMapTable.bitdepth == image_bitpix)\
            .filter(IndiAllSkyDbBadPixelMapTable.gain == self.gain_v.value)\
            .filter(IndiAllSkyDbBadPixelMapTable.binmode == self.bin_v.value)\
            .filter(IndiAllSkyDbBadPixelMapTable.exposure >= exposure)\
            .filter(IndiAllSkyDbBadPixelMapTable.temp >= self.sensortemp_v.value)\
            .filter(IndiAllSkyDbBadPixelMapTable.temp <= (self.sensortemp_v.value + self.dark_temperature_range))\
            .order_by(
                IndiAllSkyDbBadPixelMapTable.exposure.asc(),
                IndiAllSkyDbBadPixelMapTable.temp.asc(),
                IndiAllSkyDbBadPixelMapTable.createDate.asc(),
            )\
            .first()

        if not bpm_entry:
            logger.warning('Temperature matched bad pixel map not found: %0.2fc', self.sensortemp_v.value)

            # pick a bad pixel map that matches the exposure at the hightest temperature found
            bpm_entry = IndiAllSkyDbBadPixelMapTable.query\
                .filter(IndiAllSkyDbBadPixelMapTable.camera_id == camera_id)\
                .filter(IndiAllSkyDbBadPixelMapTable.bitdepth == image_bitpix)\
                .filter(IndiAllSkyDbBadPixelMapTable.gain == self.gain_v.value)\
                .filter(IndiAllSkyDbBadPixelMapTable.binmode == self.bin_v.value)\
                .filter(IndiAllSkyDbBadPixelMapTable.exposure >= exposure)\
                .order_by(
                    IndiAllSkyDbBadPixelMapTable.exposure.asc(),
                    IndiAllSkyDbBadPixelMapTable.temp.desc(),
                    IndiAllSkyDbBadPixelMapTable.createDate.asc(),
                )\
                .first()


            if not bpm_entry:
                logger.warning(
                    'Bad Pixel Map not found: ccd%d %dbit %0.7fs gain %d bin %d %0.2fc',
                    camera_id,
                    image_bitpix,
                    float(exposure),
                    self.gain_v.value,
                    self.bin_v.value,
                    self.sensortemp_v.value,
                )


        # pick a dark frame that is closest to the exposure and temperature
        logger.info('Searching for dark frame: gain %d, exposure >= %0.1f, temp >= %0.1fc', self.gain_v.value, exposure, self.sensortemp_v.value)
        dark_frame_entry = IndiAllSkyDbDarkFrameTable.query\
            .filter(IndiAllSkyDbDarkFrameTable.camera_id == camera_id)\
            .filter(IndiAllSkyDbDarkFrameTable.bitdepth == image_bitpix)\
            .filter(IndiAllSkyDbDarkFrameTable.gain == self.gain_v.value)\
            .filter(IndiAllSkyDbDarkFrameTable.binmode == self.bin_v.value)\
            .filter(IndiAllSkyDbDarkFrameTable.exposure >= exposure)\
            .filter(IndiAllSkyDbDarkFrameTable.temp >= self.sensortemp_v.value)\
            .filter(IndiAllSkyDbDarkFrameTable.temp <= (self.sensortemp_v.value + self.dark_temperature_range))\
            .order_by(
                IndiAllSkyDbDarkFrameTable.exposure.asc(),
                IndiAllSkyDbDarkFrameTable.temp.asc(),
                IndiAllSkyDbDarkFrameTable.createDate.asc(),
            )\
            .first()

        if not dark_frame_entry:
            logger.warning('Temperature matched dark not found: %0.2fc', self.sensortemp_v.value)

            # pick a dark frame that matches the exposure at the hightest temperature found
            dark_frame_entry = IndiAllSkyDbDarkFrameTable.query\
                .filter(IndiAllSkyDbDarkFrameTable.camera_id == camera_id)\
                .filter(IndiAllSkyDbDarkFrameTable.bitdepth == image_bitpix)\
                .filter(IndiAllSkyDbDarkFrameTable.gain == self.gain_v.value)\
                .filter(IndiAllSkyDbDarkFrameTable.binmode == self.bin_v.value)\
                .filter(IndiAllSkyDbDarkFrameTable.exposure >= exposure)\
                .order_by(
                    IndiAllSkyDbDarkFrameTable.exposure.asc(),
                    IndiAllSkyDbDarkFrameTable.temp.desc(),
                    IndiAllSkyDbDarkFrameTable.createDate.asc(),
                )\
                .first()


            if not dark_frame_entry:
                logger.warning(
                    'Dark not found: ccd%d %dbit %0.7fs gain %d bin %d %0.2fc',
                    camera_id,
                    image_bitpix,
                    float(exposure),
                    self.gain_v.value,
                    self.bin_v.value,
                    self.sensortemp_v.value,
                )

                raise CalibrationNotFound('Dark not found')


        if bpm_entry:
            p_bpm = Path(bpm_entry.filename)
            if p_bpm.exists():
                logger.info('Matched bad pixel map: %s', p_bpm)
                with fits.open(p_bpm) as bpm_f:
                    bpm = bpm_f[0].data
            else:
                logger.error('Bad Pixel Map missing: %s', bpm_entry.filename)
                bpm = None
        else:
            bpm = None


        p_dark_frame = Path(dark_frame_entry.filename)
        if not p_dark_frame.exists():
            logger.error('Dark file missing: %s', dark_frame_entry.filename)
            raise CalibrationNotFound('Dark file missing: {0:s}'.format(dark_frame_entry.filename))


        logger.info('Matched dark: %s', p_dark_frame)

        with fits.open(p_dark_frame) as dark_f:
            dark = dark_f[0].data


        if not isinstance(bpm, type(None)):
            # merge bad pixel map and dark
            master_dark = numpy.maximum(bpm, dark)
        else:
            master_dark = dark


        data_calibrated = cv2.subtract(data, master_dark)

        return data_calibrated


    def calculateSqm(self):
        i_ref = self.getLatestImage()

        if self.focus_mode:
            # disable processing in focus mode
            i_ref['sqm_value'] = 0
            return

        i_ref['sqm_value'] = self._sqm.calculate(i_ref['hdulist'][0].data, i_ref['exposure'], self.gain_v.value)


    def stack(self):
        i_ref = self.getLatestImage()


        if self.focus_mode:
            # disable processing in focus mode
            self.image = i_ref['hdulist'][0].data
            return


        stack_i_ref_list = list()
        for i in self.image_list:
            if isinstance(i, type(None)):
                continue

            stack_i_ref_list.append(i)


        stack_list_len = len(stack_i_ref_list)
        assert stack_list_len > 0  # canary

        if stack_list_len == 1:
            # no reason to stack a single image
            self.image = i_ref['hdulist'][0].data
            return


        image_bitpix = i_ref['image_bitpix']


        if image_bitpix == 16:
            numpy_type = numpy.uint16
        elif image_bitpix == 8:
            numpy_type = numpy.uint8
        else:
            raise Exception('Unknown bits per pixel')


        stacker = ImageStacker()


        if self.config.get('IMAGE_STACK_ALIGN') and i_ref['exposure'] > self.registration_exposure_thresh:
            # only perform registration once the exposure exceeds 5 seconds

            stack_i_ref_list = list(filter(lambda x: x['exposure'] > self.registration_exposure_thresh, stack_i_ref_list))
            stack_data_list = stacker.register(stack_i_ref_list)
        else:
            # stack unaligned images
            stack_data_list = [x['hdulist'][0].data for x in stack_i_ref_list]


        stack_start = time.time()


        try:
            stacker_method = getattr(stacker, self.stack_method)
            self.image = stacker_method(stack_data_list, numpy_type)
        except AttributeError:
            logger.error('Unknown stacking method: %s', self.stack_method)
            self.image = i_ref['hdulist'][0].data
            return


        if self.config.get('IMAGE_STACK_SPLIT'):
            self.image = self._splitscreen(i_ref['hdulist'][0].data, self.image)


        stack_elapsed_s = time.time() - stack_start
        logger.info('Stacked %d images (%s) in %0.4f s', len(stack_data_list), self.stack_method, stack_elapsed_s)


    def debayer(self):
        i_ref = self.getLatestImage()

        # sanity check
        if not len(self.image.shape) == 2:
            # already debayered
            return


        i_ref = self.getLatestImage()
        image_bayerpat = i_ref['image_bayerpat']


        if not image_bayerpat:
            logger.error('No bayer pattern detected')
            return


        if self.config.get('NIGHT_GRAYSCALE') and self.night_v.value:
            debayer_algorithm = self.__cfa_gray_map[image_bayerpat]
        elif self.config.get('DAYTIME_GRAYSCALE') and not self.night_v.value:
            debayer_algorithm = self.__cfa_gray_map[image_bayerpat]
        else:
            debayer_algorithm = self.__cfa_bgr_map[image_bayerpat]

        debayered_data_bgr = cv2.cvtColor(self.image, debayer_algorithm)
        self.image = debayered_data_bgr


    def convert_16bit_to_8bit(self):
        i_ref = self.getLatestImage()

        self._convert_16bit_to_8bit(i_ref['image_bitpix'], i_ref['image_bit_depth'])


    def _convert_16bit_to_8bit(self, image_bitpix, image_bit_depth):
        if image_bitpix == 8:
            return

        logger.info('Resampling image from %d to 8 bits', image_bitpix)

        div_factor = int((2 ** image_bit_depth) / 255)

        self.image = (self.image / div_factor).astype(numpy.uint8)


    def rotate(self, rotate_enum):
        self.image = cv2.rotate(self.image, rotate_enum)


    def flip(self, cv2_axis):
        self.image = cv2.flip(self.image, cv2_axis)


    def detectLines(self):
        i_ref = self.getLatestImage()

        if self.focus_mode:
            # disable processing in focus mode
            i_ref['lines'] = list
            return

        i_ref['lines'] = self._lineDetect.detectLines(self.image)


    def detectStars(self):
        i_ref = self.getLatestImage()

        if self.focus_mode:
            # disable processing in focus mode
            i_ref['stars'] = list()
            return

        i_ref['stars'] = self._stars.detectObjects(self.image)


    def drawDetections(self):
        if self.focus_mode:
            # disable processing in focus mode
            return

        self.image = self._draw.main(self.image)


    def crop_image(self):
        # divide the coordinates by binning value
        x1 = int(self.config['IMAGE_CROP_ROI'][0] / self.bin_v.value)
        y1 = int(self.config['IMAGE_CROP_ROI'][1] / self.bin_v.value)
        x2 = int(self.config['IMAGE_CROP_ROI'][2] / self.bin_v.value)
        y2 = int(self.config['IMAGE_CROP_ROI'][3] / self.bin_v.value)


        self.image = self.image[
            y1:y2,
            x1:x2,
        ]

        new_height, new_width = self.image.shape[:2]
        logger.info('New cropped size: %d x %d', new_width, new_height)


    def scnr(self, algo):
        if self.focus_mode:
            # disable processing in focus mode
            return

        try:
            scnr_function = getattr(self._scnr, algo)
            self.image = scnr_function(self.image)
        except AttributeError:
            logger.error('Unknown SCNR algorithm: %s', algo)


    def white_balance_manual_bgr(self):
        if self.focus_mode:
            # disable processing in focus mode
            return


        if len(self.image.shape) == 2:
            # mono
            return


        if not self.config.get('WBB_FACTOR'):
            logger.error('Missing WBB_FACTOR setting')
            return

        if not self.config.get('WBG_FACTOR'):
            logger.error('Missing WBG_FACTOR setting')
            return

        if not self.config.get('WBR_FACTOR'):
            logger.error('Missing WBR_FACTOR setting')
            return

        WBB_FACTOR = float(self.config.get('WBB_FACTOR'))
        WBG_FACTOR = float(self.config.get('WBG_FACTOR'))
        WBR_FACTOR = float(self.config.get('WBR_FACTOR'))

        b, g, r = cv2.split(self.image)

        logger.info('Applying manual color balance settings')
        wbb = cv2.multiply(b, WBB_FACTOR)
        wbg = cv2.multiply(g, WBG_FACTOR)
        wbr = cv2.multiply(r, WBR_FACTOR)

        self.image = cv2.merge([wbb, wbg, wbr])


    #def white_balance_bgr_2(self):
    #    if len(self.image.shape) == 2:
    #        # mono
    #        return

    #    lab = cv2.cvtColor(self.image, cv2.COLOR_BGR2LAB)
    #    avg_a = numpy.average(lab[:, :, 1])
    #    avg_b = numpy.average(lab[:, :, 2])
    #    lab[:, :, 1] = lab[:, :, 1] - ((avg_a - 128) * (lab[:, :, 0] / 255.0) * 1.1)
    #    lab[:, :, 2] = lab[:, :, 2] - ((avg_b - 128) * (lab[:, :, 0] / 255.0) * 1.1)
    #    self.image = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


    #def median_blur(self):
    #    if self.focus_mode:
    #        # disable processing in focus mode
    #        return

    #    data_blur = cv2.medianBlur(self.image, ksize=3)
    #    self.image = data_blur


    #def fastDenoise(self):
    #    if self.focus_mode:
    #        # disable processing in focus mode
    #        return

    #    data_denoise = cv2.fastNlMeansDenoisingColored(
    #        self.image,
    #        None,
    #        h=3,
    #        hColor=3,
    #        templateWindowSize=7,
    #        searchWindowSize=21,
    #    )

    #    self.image = data_denoise


    def white_balance_auto_bgr(self):
        if self.focus_mode:
            # disable processing in focus mode
            return

        if len(self.image.shape) == 2:
            # mono
            return

        ### This seems to work
        b, g, r = cv2.split(self.image)
        b_avg = cv2.mean(b)[0]
        g_avg = cv2.mean(g)[0]
        r_avg = cv2.mean(r)[0]

        # Find the gain of each channel
        k = (b_avg + g_avg + r_avg) / 3

        try:
            kb = k / b_avg
        except ZeroDivisionError:
            kb = k / 0.1

        try:
            kg = k / g_avg
        except ZeroDivisionError:
            kg = k / 0.1

        try:
            kr = k / r_avg
        except ZeroDivisionError:
            kr = k / 0.1

        b = cv2.addWeighted(src1=b, alpha=kb, src2=0, beta=0, gamma=0)
        g = cv2.addWeighted(src1=g, alpha=kg, src2=0, beta=0, gamma=0)
        r = cv2.addWeighted(src1=r, alpha=kr, src2=0, beta=0, gamma=0)

        self.image = cv2.merge([b, g, r])


    def contrast_clahe(self):
        if self.focus_mode:
            # disable processing in focus mode
            return

        ### ohhhh, contrasty
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))

        if len(self.image.shape) == 2:
            # mono
            self.image = clahe.apply(self.image)
            return

        # color, apply to luminance
        lab = cv2.cvtColor(self.image, cv2.COLOR_BGR2LAB)

        l, a, b = cv2.split(lab)

        cl = clahe.apply(l)

        new_lab = cv2.merge((cl, a, b))

        self.image = cv2.cvtColor(new_lab, cv2.COLOR_LAB2BGR)


    #def equalizeHistogram(self, data):
    #    if self.focus_mode:
    #        # disable processing in focus mode
    #        return

    #    if len(data.shape) == 2:
    #        # mono
    #        return cv2.equalizeHist(data)

    #    # color, apply to luminance
    #    lab = cv2.cvtColor(data, cv2.COLOR_BGR2LAB)

    #    l, a, b = cv2.split(lab)

    #    cl = cv2.equalizeHist(l)

    #    new_lab = cv2.merge((cl, a, b))

    #    return cv2.cvtColor(new_lab, cv2.COLOR_LAB2BGR)


    #def equalizeHistogramColor(self, data):
    #    if self.focus_mode:
    #        # disable processing in focus mode
    #        return

    #    if len(data.shape) == 2:
    #        # mono
    #        return data_bytes

    #    ycrcb_img = cv2.cvtColor(data, cv2.COLOR_BGR2YCrCb)
    #    ycrcb_img[:, :, 0] = cv2.equalizeHist(ycrcb_img[:, :, 0])
    #    return cv2.cvtColor(ycrcb_img, cv2.COLOR_YCrCb2BGR)


    def scale_image(self):
        if self.focus_mode:
            # disable processing in focus mode
            return

        image_height, image_width = self.image.shape[:2]

        logger.info('Scaling image by %d%%', self.config['IMAGE_SCALE'])
        new_width = int(image_width * self.config['IMAGE_SCALE'] / 100.0)
        new_height = int(image_height * self.config['IMAGE_SCALE'] / 100.0)

        logger.info('New size: %d x %d', new_width, new_height)

        self.image = cv2.resize(self.image, (new_width, new_height), interpolation=cv2.INTER_AREA)


    def _splitscreen(self, original_data, stacked_data):
        # if flip horizontal is set, this data will swap sides later
        if self.config.get('IMAGE_FLIP_H'):
            left_data = stacked_data
            right_data = original_data
        else:
            left_data = original_data
            right_data = stacked_data


        image_height, image_width = left_data.shape[:2]

        half_width = int(image_width / 2)

        # left side
        left_mask = numpy.zeros((image_height, image_width), dtype=numpy.uint8)
        cv2.rectangle(
            img=left_mask,
            pt1=(0, 0),
            #pt2=(half_width, image_height),
            pt2=(half_width - 1, image_height),  # ensure a black line is down the center
            color=255,
            thickness=cv2.FILLED,
        )

        masked_left = cv2.bitwise_and(left_data, left_data, mask=left_mask)

        # right side
        right_mask = numpy.zeros((image_height, image_width), dtype=numpy.uint8)
        cv2.rectangle(
            img=right_mask,
            pt1=(half_width + 1, 0),
            pt2=(image_width, image_height),
            color=255,
            thickness=cv2.FILLED,
        )

        masked_right = cv2.bitwise_and(right_data, right_data, mask=right_mask)

        return numpy.maximum(masked_left, masked_right)


    def image_text(self):
        # this needs to be enabled during focus mode

        i_ref = self.getLatestImage()

        self._image_text(i_ref)


    def _image_text(self, i_ref):
        # Legacy setting, code to be removed later
        if not self.config['TEXT_PROPERTIES'].get('FONT_FACE'):
            logger.warning('Image labels disabled')
            return

        # Image labels are enabled by default
        if not self.config.get('IMAGE_LABEL', True):
            logger.warning('Image labels disabled')
            return


        image_height, image_width = self.image.shape[:2]

        color_bgr = list(self.config['TEXT_PROPERTIES']['FONT_COLOR'])
        color_bgr.reverse()


        # Disabled when focus mode is enabled
        if self.config.get('FOCUS_MODE', False):
            logger.warning('Focus mode enabled, Image labels disabled')

            # indicate focus mode is enabled in indi-allsky
            self.drawText(
                self.image,
                i_ref['exp_date'].strftime('%H:%M:%S'),
                (image_width - 125, image_height - 10),
                tuple(color_bgr),
            )

            return


        utcnow = datetime.utcnow()  # ephem expects UTC dates
        #utcnow = datetime.utcnow() - timedelta(hours=13)  # testing

        obs = ephem.Observer()
        obs.lon = math.radians(self.longitude_v.value)
        obs.lat = math.radians(self.latitude_v.value)


        sun = ephem.Sun()
        obs.date = utcnow
        sun.compute(obs)
        self.astrometric_data['sun_alt'] = math.degrees(sun.alt)



        moon = ephem.Moon()
        #obs.date = utcnow
        moon.compute(obs)
        self.astrometric_data['moon_alt'] = math.degrees(moon.alt)
        self.astrometric_data['moon_phase'] = moon.moon_phase * 100.0


        # separation of 1-3 degrees means a possible eclipse
        self.astrometric_data['sun_moon_sep'] = abs((ephem.separation(moon, sun) / (math.pi / 180)) - 180)


        self.astrometric_data['sidereal_time'] = str(obs.sidereal_time())


        ### ORBS
        orb_mode = self.config.get('ORB_PROPERTIES', {}).get('MODE', 'ha')
        if orb_mode == 'ha':
            self._orb.drawOrbsHourAngle(self.image, utcnow, color_bgr, obs, sun, moon)
        elif orb_mode == 'az':
            self._orb.drawOrbsAzimuth(self.image, utcnow, color_bgr, obs, sun, moon)
        elif orb_mode == 'alt':
            self._orb.drawOrbsAltitude(self.image, utcnow, color_bgr, obs, sun, moon)
        elif orb_mode == 'off':
            # orbs disabled
            pass
        else:
            logger.error('Unknown orb display mode: %s', orb_mode)



        image_label_tmpl = self.config.get('IMAGE_LABEL_TEMPLATE', '{timestamp:%Y%m%d %H:%M:%S}\nExposure {exposure:0.6f}\nGain {gain:d}\nTemp {temp:0.1f}{temp_unit:s}\nStars {stars:d}')


        if self.config.get('TEMP_DISPLAY') == 'f':
            sensortemp = ((self.sensortemp_v.value * 9.0) / 5.0) + 32
            temp_unit = 'F'
        elif self.config.get('TEMP_DISPLAY') == 'k':
            sensortemp = self.sensortemp_v.value + 273.15
            temp_unit = 'K'
        else:
            sensortemp = self.sensortemp_v.value
            temp_unit = 'C'


        label_data = {
            'timestamp'    : i_ref['exp_date'],
            'ts'           : i_ref['exp_date'],  # shortcut
            'exposure'     : i_ref['exposure'],
            'gain'         : self.gain_v.value,
            'temp'         : sensortemp,  # hershey fonts do not support degree symbol
            'temp_unit'    : temp_unit,
            'sqm'          : i_ref['sqm_value'],
            'stars'        : len(i_ref['stars']),
            'detections'   : str(bool(len(i_ref['lines']))),
            'sun_alt'      : self.astrometric_data['sun_alt'],
            'moon_alt'     : self.astrometric_data['moon_alt'],
            'moon_phase'   : self.astrometric_data['moon_phase'],
            'sun_moon_sep' : self.astrometric_data['sun_moon_sep'],
            'latitude'     : self.latitude_v.value,
            'longitude'    : self.longitude_v.value,
            'sidereal_time': self.astrometric_data['sidereal_time'],
        }


        # stacking data
        if self.night_v.value:
            if self.config.get('IMAGE_STACK_COUNT', 1) > 1:
                label_data['stack_method'] = self.config.get('IMAGE_STACK_METHOD', 'average').capitalize()
                label_data['stack_count'] = self.config.get('IMAGE_STACK_COUNT', 1)
            else:
                label_data['stack_method'] = 'Off'
                label_data['stack_count'] = 0
        else:
            # stacking disabled during the day
            label_data['stack_method'] = 'Off'
            label_data['stack_count'] = 0


        image_label = image_label_tmpl.format(**label_data)  # fill in the data


        line_offset = 0
        for line in image_label.split('\n'):
            self.drawText(
                self.image,
                line,
                (self.config['TEXT_PROPERTIES']['FONT_X'], self.config['TEXT_PROPERTIES']['FONT_Y'] + line_offset),
                tuple(color_bgr),
            )

            line_offset += self.config['TEXT_PROPERTIES']['FONT_HEIGHT']


        # Add moon mode indicator
        if self.moonmode_v.value:
            self.drawText(
                self.image,
                '* Moon Mode *',
                (self.config['TEXT_PROPERTIES']['FONT_X'], self.config['TEXT_PROPERTIES']['FONT_Y'] + line_offset),
                tuple(color_bgr),
            )

            line_offset += self.config['TEXT_PROPERTIES']['FONT_HEIGHT']


        # Add eclipse indicator
        if self.astrometric_data['sun_moon_sep'] < 1.25 and self.night_v.value:
            # Lunar eclipse (earth's penumbra is large)
            self.drawText(
                self.image,
                '* LUNAR ECLIPSE *',
                (self.config['TEXT_PROPERTIES']['FONT_X'], self.config['TEXT_PROPERTIES']['FONT_Y'] + line_offset),
                tuple(color_bgr),
            )

            line_offset += self.config['TEXT_PROPERTIES']['FONT_HEIGHT']

        elif self.astrometric_data['sun_moon_sep'] > 179.0 and not self.night_v.value:
            # Solar eclipse
            self.drawText(
                self.image,
                '* SOLAR ECLIPSE *',
                (self.config['TEXT_PROPERTIES']['FONT_X'], self.config['TEXT_PROPERTIES']['FONT_Y'] + line_offset),
                tuple(color_bgr),
            )

            line_offset += self.config['TEXT_PROPERTIES']['FONT_HEIGHT']


        # add extra text to image
        extra_text_lines = self.get_extra_text()
        if extra_text_lines:
            logger.info('Adding extra text from %s', self.config['IMAGE_EXTRA_TEXT'])

            for extra_text_line in extra_text_lines:
                self.drawText(
                    self.image,
                    extra_text_line,
                    (self.config['TEXT_PROPERTIES']['FONT_X'], self.config['TEXT_PROPERTIES']['FONT_Y'] + line_offset),
                    tuple(color_bgr),
                )

                line_offset += self.config['TEXT_PROPERTIES']['FONT_HEIGHT']


    def drawText(self, data, text, pt, color_bgr):
        fontFace = getattr(cv2, self.config['TEXT_PROPERTIES']['FONT_FACE'])
        lineType = getattr(cv2, self.config['TEXT_PROPERTIES']['FONT_AA'])

        if self.config['TEXT_PROPERTIES']['FONT_OUTLINE']:
            cv2.putText(
                img=data,
                text=text,
                org=pt,
                fontFace=fontFace,
                color=(0, 0, 0),
                lineType=lineType,
                fontScale=self.config['TEXT_PROPERTIES']['FONT_SCALE'],
                thickness=self.config['TEXT_PROPERTIES']['FONT_THICKNESS'] + 1,
            )  # black outline
        cv2.putText(
            img=data,
            text=text,
            org=pt,
            fontFace=fontFace,
            color=tuple(color_bgr),
            lineType=lineType,
            fontScale=self.config['TEXT_PROPERTIES']['FONT_SCALE'],
            thickness=self.config['TEXT_PROPERTIES']['FONT_THICKNESS'],
        )


    def get_extra_text(self):
        if not self.config.get('IMAGE_EXTRA_TEXT'):
            return list()


        image_extra_text_p = Path(self.config['IMAGE_EXTRA_TEXT'])

        try:
            if not image_extra_text_p.exists():
                logger.error('%s does not exist', image_extra_text_p)
                return list()


            if not image_extra_text_p.is_file():
                logger.error('%s is not a file', image_extra_text_p)
                return list()


            # Sanity check
            if image_extra_text_p.stat().st_size > 10000:
                logger.error('%s is too large', image_extra_text_p)
                return list()

        except PermissionError as e:
            logger.error(str(e))
            return list()


        try:
            with io.open(str(image_extra_text_p), 'r') as image_extra_text_f:
                extra_lines = [x.rstrip() for x in image_extra_text_f.readlines()]
                image_extra_text_f.close()
        except PermissionError as e:
            logger.error(str(e))
            return list()


        return extra_lines


class ImageStacker(object):

    def mean(self, *args, **kwargs):
        # alias for average
        return self.average(*args, **kwargs)


    def average(self, stack_data_list, numpy_type):
        mean_image = numpy.mean(stack_data_list, axis=0)
        return numpy.floor(mean_image).astype(numpy_type)  # no floats


    def maximum(self, stack_data_list, numpy_type):
        image_max = stack_data_list[0]  # start with first image

        # compare with remaining images
        for i in stack_data_list[1:]:
            image_max = numpy.maximum(image_max, i)

        return image_max

    def minimum(self, stack_data_list, numpy_type):
        image_min = stack_data_list[0]  # start with first image

        # compare with remaining images
        for i in stack_data_list[1:]:
            image_min = numpy.minimum(image_min, i)

        return image_min


    def register(self, stack_i_ref_list):
        reference_i_ref = stack_i_ref_list[0]

        reg_data_list = [reference_i_ref['hdulist'][0].data]  # add target to final list

        reference_crop = self._crop(reference_i_ref['hdulist'][0].data)

        reg_start = time.time()

        for i_ref in stack_i_ref_list[1:]:
            i_crop = self._crop = i_ref['hdulist'][0].data

            # detection_sigma default = 5
            # max_control_points default = 50
            # min_area default = 5

            try:
                ### Find transform using a crop of the image
                transform, (source_list, target_list) = astroalign.find_transform(
                    i_crop,
                    reference_crop,
                    detection_sigma=7,
                    max_control_points=100,
                    min_area=15,
                )

                reg_data, footprint = astroalign.apply_transform(
                    transform,
                    i_ref['hdulist'][0],
                    reference_i_ref['hdulist'][0],
                )

                ### Register full image
                #reg_data, footprint = astroalign.register(
                #    i_ref['hdulist'][0],
                #    reference_i_ref['hdulist'][0],
                #    detection_sigma=7,
                #    max_control_points=100,
                #    min_area=15,
                #)
            except astroalign.MaxIterError as e:
                logger.error('Image registration failure: %s', str(e))
                continue
            except ValueError as e:
                logger.error('Image registration failure: %s', str(e))
                continue

            reg_data_list.append(reg_data)


        reg_elapsed_s = time.time() - reg_start
        logger.info('Registered %d+1 images in %0.4f s', len(stack_i_ref_list) - 1, reg_elapsed_s)  # reference image is not aligned

        return reg_data_list


    def _crop(self, image):
        image_height, image_width = image.shape[:2]

        x1 = int((image_width / 3) - (image_width / 3))
        y1 = int((image_height / 3) - (image_height / 3))
        x2 = int((image_width / 3) + (image_width / 3))
        y2 = int((image_height / 3) + (image_height / 3))


        return image[
            y1:y2,
            x1:x2,
        ]

