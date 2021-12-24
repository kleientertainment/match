from elasticsearch import Elasticsearch
from flask import Flask, request
from image_match.elasticsearch_driver import SignatureES
from image_match.goldberg import ImageSignature
from PIL import Image
import json
import os
import sys
import io
import urllib.request
import logging

# =============================================================================
# Globals

es_url = os.environ['ELASTICSEARCH_URL']
es_index = os.environ['ELASTICSEARCH_INDEX']
es_doc_type = os.environ['ELASTICSEARCH_DOC_TYPE']
all_orientations = os.environ['ALL_ORIENTATIONS']
image_dimentions_limit = os.getenv('IMAGE_DIMENTIONS_LIMIT')    

app = Flask(__name__)
es = Elasticsearch([es_url], verify_certs=True, timeout=60, max_retries=10, retry_on_timeout=True)
ses = SignatureES(es, index=es_index, doc_type=es_doc_type)
gis = ImageSignature()

# Try to create the index and ignore IndexAlreadyExistsException
# if the index already exists
es.indices.create(index=es_index, ignore=400)

if __name__ != '__main__':
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)

# =============================================================================
# Helpers

def ids_with_path(path):
    matches = es.search(index=es_index,
                        _source='_id',
                        q='path:' + json.dumps(path))
    return [m['_id'] for m in matches['hits']['hits']]

def paths_at_location(offset, limit):
    search = es.search(index=es_index,
                       from_=offset,
                       size=limit,
                       _source='path')
    return [h['_source']['path'] for h in search['hits']['hits']]

def count_images():
    return es.count(index=es_index)['count']

def delete_ids(ids):
    for i in ids:
        es.delete(index=es_index, doc_type=es_doc_type, id=i, ignore=404)

def dist_to_percent(dist):
    return (1 - dist) * 100

def get_image(url_field, file_field):
    global image_dimentions_limit
    if url_field in request.form:
        response = urllib.request.urlopen(request.form[url_field])
        
        img = Image.open(io.BytesIO(response.read()))

        image_width = img.size[0]
        image_height = img.size[1]
        image_extension = img.format

        app.logger.info('Received image with dimentions: %sx%s with format %s', image_width, image_height, image_extension)

        if image_dimentions_limit is not None:
            image_dimentions_limit = int(image_dimentions_limit)
            app.logger.info('Image dimentions limit %spx', image_dimentions_limit)
            if image_width > image_dimentions_limit or image_height > image_dimentions_limit:
                if image_width > image_height:
                    wpercent = (image_dimentions_limit / float(image_width))
                    hsize = int((float(image_height) * float(wpercent)))
                    img = img.resize((image_dimentions_limit, hsize), Image.ANTIALIAS)
                else:
                    hpercent = (image_dimentions_limit / float(image_height))
                    wsize = int((float(image_width) * float(hpercent)))
                    img = img.resize((wsize, image_dimentions_limit), Image.ANTIALIAS)
            app.logger.info('Image resized: %sx%s', img.size[0], img.size[1])                
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format=image_extension)
        return img_byte_arr.getvalue(), True
    else:
        return request.files[file_field].read(), True

# =============================================================================
# Routes

@app.route('/add', methods=['POST'])
def add_handler():
    path = request.form['filepath']
    try:
        metadata = json.loads(request.form['metadata'])
    except KeyError:
        metadata = None
    img, bs = get_image('url', 'image')

    old_ids = ids_with_path(path)
    ses.add_image(path, img, bytestream=bs, metadata=metadata)
    delete_ids(old_ids)

    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'add',
        'result': []
    })

@app.route('/delete', methods=['DELETE'])
def delete_handler():
    path = request.form['filepath']
    ids = ids_with_path(path)
    delete_ids(ids)
    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'delete',
        'result': []
    })

@app.route('/search', methods=['POST'])
def search_handler():
    img, bs = get_image('url', 'image')
    ao = request.form.get('all_orientations', all_orientations) == 'true'

    matches = ses.search_image(
            path=img,
            all_orientations=ao,
            bytestream=bs)

    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'search',
        'result': [{
            'score': dist_to_percent(m['dist']),
            'filepath': m['path'],
            'metadata': m['metadata']
        } for m in matches]
    })

@app.route('/compare', methods=['POST'])
def compare_handler():
    img1, bs1 = get_image('url1', 'image1')
    img2, bs2 = get_image('url2', 'image2')
    img1_sig = gis.generate_signature(img1, bytestream=bs1)
    img2_sig = gis.generate_signature(img2, bytestream=bs2)
    score = dist_to_percent(gis.normalized_distance(img1_sig, img2_sig))

    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'compare',
        'result': [{ 'score': score }]
    })

@app.route('/count', methods=['GET', 'POST'])
def count_handler():
    count = count_images()
    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'count',
        'result': [count]
    })

@app.route('/list', methods=['GET', 'POST'])
def list_handler():
    if request.method == 'GET':
        offset = max(int(request.args.get('offset', 0)), 0)
        limit = max(int(request.args.get('limit', 20)), 0)
    else:
        offset = max(int(request.form.get('offset', 0)), 0)
        limit = max(int(request.form.get('limit', 20)), 0)
    paths = paths_at_location(offset, limit)

    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'list',
        'result': paths
    })

@app.route('/ping', methods=['GET', 'POST'])
def ping_handler():
    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'ping',
        'result': []
    })

# =============================================================================
# Error Handling

@app.errorhandler(400)
def bad_request(e):
    return json.dumps({
        'status': 'fail',
        'error': ['bad request'],
        'method': '',
        'result': []
    }), 400

@app.errorhandler(404)
def page_not_found(e):
    return json.dumps({
        'status': 'fail',
        'error': ['not found'],
        'method': '',
        'result': []
    }), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return json.dumps({
        'status': 'fail',
        'error': ['method not allowed'],
        'method': '',
        'result': []
    }), 405

@app.errorhandler(500)
def server_error(e):
    return json.dumps({
        'status': 'fail',
        'error': [str(e)],
        'method': '',
        'result': []
    }), 500
