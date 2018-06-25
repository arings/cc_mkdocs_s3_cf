import pygit2
import json
import boto3
import zipfile
import os
import datetime
import shutil
import mimetypes
import hashlib
import re
import botocore
from pygit2.remote import RemoteCallbacks
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.compat import urlsplit
from botocore.exceptions import ClientError
from subprocess import call

REGION = os.getenv('AWS_REGION')
S3_BUCKET = os.getenv('S3_BUCKET', '')
BRANCH = os.getenv('BRANCH', 'master')
SITE_ROOT_DIR = 'site'
codecommit = boto3.client('codecommit', region_name=REGION)
session = boto3.Session()

s3 = session.resource('s3')
bucket = s3.Bucket(S3_BUCKET)


def sign_request(region, url_to_sign):
    credentials = session.get_credentials()
    signer = SigV4Auth(credentials, 'codecommit', region)
    request = AWSRequest()
    request.url = url_to_sign
    request.method = 'GIT'
    now = datetime.datetime.utcnow()
    request.context['timestamp'] = now.strftime('%Y%m%dT%H%M%S')
    split = urlsplit(request.url)
    # we don't want to include the port number in the signature
    hostname = split.netloc.split(':')[0]
    canonical_request = '{0}\n{1}\n\nhost:{2}\n\nhost\n'.format(
        request.method, split.path, hostname)
    print 'CanonicalRequest:\n%s' % canonical_request
    string_to_sign = signer.string_to_sign(request, canonical_request)
    print 'StringToSign:\n%s' % string_to_sign
    signature = signer.signature(string_to_sign, request)
    print 'Signature:\n%s' % signature

    return '{0}Z{1}'.format(request.context['timestamp'], signature)


def get_cred(repo_url, repo_region):
    credentials = session.get_credentials()
    credentials = credentials.get_frozen_credentials()

    username = credentials.access_key + '%' + credentials.token

    password = sign_request(repo_region, repo_url)

    cred = pygit2.UserPass(username, password)

    print credentials

    return RemoteCallbacks(credentials=cred)


def clone(repo_url, repo_region, repo_path):
    print repo_url
    print repo_region
    print repo_path

    repo = pygit2.clone_repository(
        repo_url,
        repo_path,
        checkout_branch=BRANCH,
        callbacks=get_cred(repo_url, repo_region))

def initialize():
    for root, dirs, files in os.walk('/tmp'):
        for f in files:
            os.unlink(os.path.join(root, f))
        for d in dirs:
            shutil.rmtree(os.path.join(root, d))


def getMD5(filename):
    f = open(filename, 'rb')
    m = hashlib.md5()
    while True:
        data = f.read(10240)
        if len(data) == 0:
            break
        m.update(data)
    return m.hexdigest()


def buildDocs(repo_path):
    print("Building docs...")
    os.chdir(repo_path)
    mkdocs_path = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "mkd")
    print(mkdocs_path)
    call(["python", mkdocs_path, "build"])


def getFileList(path):
    files = []
    for root, dirnames, filenames in os.walk(path):
        for filename in filenames:
            files.append(os.path.join(root, filename)[len(path) + 1:])
    return files


def to_uri(filename):
    return re.sub(SITE_ROOT_DIR, '', filename)


def compareWithS3(files):
    print("Comparing file hashes with s3...")
    files_to_upload = []
    for f in files:
        uri = to_uri(f)
        print('uri', uri)
        try:
            md5sum = boto3.client('s3').head_object(
                Bucket=S3_BUCKET, Key=uri)['ETag'][1:-1]
        except ClientError:
            md5sum = None
            pass
        print md5sum
        if md5sum is None:
            # new file, upload
            files_to_upload.append(f)
        else:
            # check MD5
            md5 = getMD5(os.path.join(SITE_ROOT_DIR, f))
            if md5sum != md5:
                print(f + ": " + md5 + " != " + md5sum)
                files_to_upload.append(f)
    return files_to_upload


def uploadFiles(files):
    print("Uploading files to S3...")
    key_list = []
    for file in files:
        file_mime = mimetypes.guess_type(file)[0] or 'application/octet-stream'

        with open(os.path.join(SITE_ROOT_DIR, file), 'rb') as data:
            key = file
            bucket.put_object(Key=key, Body=data, ContentType=file_mime)
            key_list.append(key)

    return key_list


def invalidateFiles(keys):
    print("Invalidating files in cloudfront...")
    cloudfront = boto3.client('cloudfront')
    cloudfront.create_invalidation(
        DistributionId='E1KTJMXCM2MXLT',
        InvalidationBatch={
            'Paths': {
                'Quantity': len(keys),
                'Items': ['/{}'.format(k) for k in keys]
            },
            'CallerReference':
            'my-references-{}'.format(datetime.datetime.now())
        })


def lambda_handler(event, context):

    if S3_BUCKET == '' or S3_BUCKET == None:
        return 'Please specify the S3_BUCKET value in the environment variable.'

    # Log the updated references from the event
    references = {
        reference['ref']
        for reference in event['Records'][0]['codecommit']['references']
    }
    print("References: " + str(references))

    # Get the repository from the event and show its git clone URL
    repository = event['Records'][0]['eventSourceARN'].split(':')[5]
    try:
        # Clear the /tmp if the lambda is having concurrency requests
        initialize()

        response = codecommit.get_repository(repositoryName=repository)
        cloneUrlHttp = response['repositoryMetadata']['cloneUrlHttp']

        # Generate repository path by time
        repo_path = os.path.join('/tmp',
                                 datetime.datetime.now().strftime('%H%M%S%f'))
        clone(cloneUrlHttp, REGION, repo_path)
        buildDocs(repo_path)
        site_path = os.path.join(repo_path, SITE_ROOT_DIR)
        print('site_path', site_path)
        file_list = getFileList(site_path)
        print('file_list', file_list)
        files_to_upload = compareWithS3(file_list)
        print('files_to_upload', files_to_upload)
        keys_to_invalidate = uploadFiles(files_to_upload)
        print('keys_to_invalidate', keys_to_invalidate)
        invalidateFiles(keys_to_invalidate)
    except Exception as e:
        print(e)
        print(
            'Error getting repository {}. Make sure it exists and that your repository is in the same region as this function.'.
            format(repository))
        raise e
