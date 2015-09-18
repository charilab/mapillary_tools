from lib.exif import EXIF, exif_gps_fields, exif_datetime_fields
from lib.geo import dms_to_decimal, normalize_bearing
from lib.exifedit import ExifEdit
import lib.io

import pyexiv2
import json
import os
import datetime
import hashlib
import base64
import uuid
import string
import threading
import sys
import urllib2, urllib
import socket
import mimetypes
import random
import string
from Queue import Queue
import threading
import exifread
import time


MAPILLARY_UPLOAD_URL = "https://d22zcsn13kp53w.cloudfront.net/"
PERMISSION_HASH = "eyJleHBpcmF0aW9uIjoiMjAyMC0wMS0wMVQwMDowMDowMFoiLCJjb25kaXRpb25zIjpbeyJidWNrZXQiOiJtYXBpbGxhcnkudXBsb2Fkcy5pbWFnZXMifSxbInN0YXJ0cy13aXRoIiwiJGtleSIsIiJdLHsiYWNsIjoicHJpdmF0ZSJ9LFsic3RhcnRzLXdpdGgiLCIkQ29udGVudC1UeXBlIiwiIl0sWyJjb250ZW50LWxlbmd0aC1yYW5nZSIsMCwyMDQ4NTc2MF1dfQ=="
SIGNATURE_HASH = "f6MHj3JdEq8xQ/CmxOOS7LvMxoI="
BOUNDARY_CHARS = string.digits + string.ascii_letters
NUMBER_THREADS = int(os.getenv('NUMBER_THREADS', '4'))
MAX_ATTEMPTS = int(os.getenv('MAX_ATTEMPTS', '10'))
UPLOAD_PARAMS = {"url": MAPILLARY_UPLOAD_URL, "permission": PERMISSION_HASH, "signature": SIGNATURE_HASH, "move_files":True}

class UploadThread(threading.Thread):
    def __init__(self, queue, params=UPLOAD_PARAMS):
        threading.Thread.__init__(self)
        self.q = queue
        self.params = params

    def run(self):
        while True:
            # fetch file from the queue and upload
            filepath = self.q.get()
            if filepath is None:
                self.q.task_done()
                break
            else:
                upload_file(filepath, **self.params)
                self.q.task_done()


def create_mapillary_description(filename, username, email, upload_hash, sequence_uuid, interpolated_heading=0.0, verbose=False):
    '''
    Check that image file has the required EXIF fields.

    Incompatible files will be ignored server side.
    '''
    # read exif
    exif = EXIF(filename)

    if not verify_exif(filename):
        return False

    # write the mapillary tag
    mapillary_description = {}
    mapillary_description["MAPLongitude"], mapillary_description["MAPLatitude"] = exif.extract_lon_lat()
    #required date format: 2015_01_14_09_37_01_000
    mapillary_description["MAPCaptureTime"] = datetime.datetime.strftime(exif.extract_capture_time(), "%Y_%m_%d_%H_%M_%S_%f")[:-3]
    mapillary_description["MAPOrientation"] = exif.extract_orientation()
    heading = exif.extract_direction()
    heading = normalize_bearing(interpolated_heading) if heading is None else normalize_bearing(heading)
    mapillary_description["MAPCompassHeading"] = {"TrueHeading": heading, "MagneticHeading": heading}
    mapillary_description["MAPSettingsUploadHash"] = upload_hash
    mapillary_description["MAPSettingsEmail"] = email
    mapillary_description["MAPSettingsUsername"] = username
    settings_upload_hash = hashlib.sha256("%s%s%s" % (upload_hash, email, base64.b64encode(filename))).hexdigest()
    mapillary_description['MAPSettingsUploadHash'] = settings_upload_hash
    mapillary_description['MAPPhotoUUID'] = str(uuid.uuid4())
    mapillary_description['MAPSequenceUUID'] = str(sequence_uuid)
    mapillary_description['MAPDeviceModel'] = exif.extract_model()
    mapillary_description['MAPDeviceMake'] = exif.extract_make()

    # write to file
    json_desc = json.dumps(mapillary_description)
    if verbose:
        print "tag: {0}".format(json_desc)
    metadata = ExifEdit(filename)
    metadata.add_image_description(json_desc)
    metadata.write()


def encode_multipart(fields, files, boundary=None):
    """
    Encode dict of form fields and dict of files as multipart/form-data.
    Return tuple of (body_string, headers_dict). Each value in files is a dict
    with required keys 'filename' and 'content', and optional 'mimetype' (if
    not specified, tries to guess mime type or uses 'application/octet-stream').

    From MIT licensed recipe at
    http://code.activestate.com/recipes/578668-encode-multipart-form-data-for-uploading-files-via/
    """
    def escape_quote(s):
        return s.replace('"', '\\"')

    if boundary is None:
        boundary = ''.join(random.choice(BOUNDARY_CHARS) for i in range(30))
    lines = []

    for name, value in fields.items():
        lines.extend((
            '--{0}'.format(boundary),
            'Content-Disposition: form-data; name="{0}"'.format(escape_quote(name)),
            '',
            str(value),
        ))

    for name, value in files.items():
        filename = value['filename']
        if 'mimetype' in value:
            mimetype = value['mimetype']
        else:
            mimetype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        lines.extend((
            '--{0}'.format(boundary),
            'Content-Disposition: form-data; name="{0}"; filename="{1}"'.format(
                    escape_quote(name), escape_quote(filename)),
            'Content-Type: {0}'.format(mimetype),
            '',
            value['content'],
        ))

    lines.extend((
        '--{0}--'.format(boundary),
        '',
    ))
    body = '\r\n'.join(lines)

    headers = {
        'Content-Type': 'multipart/form-data; boundary={0}'.format(boundary),
        'Content-Length': str(len(body)),
    }
    return (body, headers)


def get_upload_token(mail, pwd):
    '''
    Get upload token
    '''
    params = urllib.urlencode({"email": mail, "password": pwd})
    response = urllib.urlopen("https://api.mapillary.com/v1/u/login", params)
    resp = json.loads(response.read())
    return resp['upload_token']


def get_authentication_info():
    '''
    Get authentication information from env
    '''
    try:
        MAPILLARY_USERNAME = os.environ['MAPILLARY_USERNAME']
        MAPILLARY_EMAIL = os.environ['MAPILLARY_EMAIL']
        MAPILLARY_PASSWORD = os.environ['MAPILLARY_PASSWORD']
    except KeyError:
        return None
    return MAPILLARY_USERNAME, MAPILLARY_EMAIL, MAPILLARY_PASSWORD


def upload_done_file(params):
    print("Upload a DONE file to tell the backend that the sequence is all uploaded and ready to submit.")
    if not os.path.exists("DONE"):
        open("DONE", 'a').close()
    #upload
    upload_file("DONE", **params)
    #remove
    if os.path.exists("DONE"):
        os.remove("DONE")


def upload_file(filepath, url, permission, signature, key=None, move_files=True):
    '''
    Upload file at filepath.

    Move to subfolders 'success'/'failed' on completion if move_files is True.
    '''
    filename = os.path.basename(filepath)
    s3_filename = EXIF(filepath).exif_name()
    print("Uploading: {0}".format(filename))

    # add S3 'path' if given
    if key is None:
        s3_key = s3_filename
    else:
        s3_key = key+s3_filename

    parameters = {"key": s3_key, "AWSAccessKeyId": "AKIAI2X3BJAT2W75HILA", "acl": "private",
                "policy": permission, "signature": signature, "Content-Type":"image/jpeg" }

    with open(filepath, "rb") as f:
        encoded_string = f.read()

    data, headers = encode_multipart(parameters, {'file': {'filename': filename, 'content': encoded_string}})

    root_path = os.path.dirname(filepath)
    success_path = os.path.join(root_path, 'success')
    failed_path = os.path.join(root_path, 'failed')
    lib.io.mkdir_p(success_path)
    lib.io.mkdir_p(failed_path)

    for attempt in range(MAX_ATTEMPTS):
        try:
            request = urllib2.Request(url, data=data, headers=headers)
            response = urllib2.urlopen(request)

            if response.getcode()==204:
                if move_files:
                    os.rename(filepath, os.path.join(success_path, filename))
                print("Success: {0}".format(filename))
            else:
                if move_files:
                    os.rename(filepath, os.path.join(failed_path,filename))
                print("Failed: {0}".format(filename))
            break # attempts

        except urllib2.HTTPError as e:
            print("HTTP error: {0} on {1}".format(e, filename))
            time.sleep(5)
        except urllib2.URLError as e:
            print("URL error: {0} on {1}".format(e, filename))
            time.sleep(5)
        except OSError as e:
            print("OS error: {0} on {1}".format(e, filename))
            time.sleep(5)
        except socket.timeout as e:
            # Specific timeout handling for Python 2.7
            print("Timeout error: {0} (retrying)".format(filename))


def upload_file_list(file_list, params):
    # create upload queue with all files
    q = Queue()
    for filepath in file_list:
        if EXIF(filepath).mapillary_tag_exists():
            q.put(filepath)
        else:
            print("Skipping: {0}".format(filepath))

    # create uploader threads
    uploaders = [UploadThread(q, params) for i in range(NUMBER_THREADS)]

    # start uploaders as daemon threads that can be stopped (ctrl-c)
    try:
        for uploader in uploaders:
            uploader.daemon = True
            uploader.start()

        for uploader in uploaders:
            uploaders[i].join(1)

        while q.unfinished_tasks:
            time.sleep(1)
        q.join()
    except (KeyboardInterrupt, SystemExit):
        print("\nBREAK: Stopping upload.")
        sys.exit()


def create_dirs(root_path=''):
    lib.io.mkdir_p(os.path.join(root_path, "success"))
    lib.io.mkdir_p(os.path.join(root_path, "failed"))


def verify_exif(filename):
    '''
    Check that image file has the required EXIF fields.

    Incompatible files will be ignored server side.
    '''

    # required tags in IFD name convention
    required_exif = exif_gps_fields() + exif_datetime_fields() + [["Image Orientation"]]
    exif = EXIF(filename)
    tags = exif.tags
    required_exif_exist = exif.fileds_exist(required_exif)
    return required_exif_exist