from __future__ import absolute_import, unicode_literals
import re
import requests
import time
from apiclient import discovery
from httplib2 import Http
from oauth2client import client, file
from celery import task, current_task, states
from celery.exceptions import Ignore

SPN_URL = 'https://web-beta.archive.org/save/'
AVAILABILITY_API_URL = 'https://archive.org/wayback/available'
CLIENT_ID = '993382127942-iakt5sui2m26t4vg0ed1g7f0kt2kch4e.apps.googleusercontent.com'
CLIENT_SECRET = '3JrJxLpmpkN3WezmwYKF4AhL'
SCOPES = 'https://www.googleapis.com/auth/spreadsheets'
REDIRECT_URI_DEV = 'http://localhost:8092/archive/'
REDIRECT_URI_PROD = 'http://anton-dev.us.archive.org:8092/archive/'

@task
def process_doc(spreadsheet_id, auth_code, headers):
    flow = client.OAuth2WebServerFlow(client_id=CLIENT_ID,
                                      client_secret=CLIENT_SECRET,
                                      scope=SCOPES,
                                      redirect_uri=REDIRECT_URI_PROD,
                                      prompt='consent',
                                      access_type='offline')

    creds = flow.step2_exchange(auth_code)
    http = creds.authorize(Http())

    service = discovery.build('sheets', 'v4', http=http, cache_discovery=False)
    spreadsheet = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets = [s['properties']['title'] for s in spreadsheet['sheets']]

    for sheet in sheets:
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=sheet + '!A3:A10000').execute()
        values = result.get('values', [])

        if not values:
            print('No data found in ', sheet)
        else:
            row_index = 2
            error_count = 0
            for value in values:
                row_index = row_index + 1

                if len(value) == 0:
                    continue

                url = value[0]

                if creds.access_token_expired or creds.invalid:
                    creds.refresh(http)
                    service = discovery.build('sheets', 'v4', http=http, cache_discovery=False)

                if not is_valid_url(url):
                    update_values(service,
                                  spreadsheet_id,
                                  sheet + '!B' + str(row_index) + ':D' + str(row_index),
                                  ['', 'Error: Invalid URL', ''])
                    error_count = error_count + 1
                    update_state(row_index, values, error_count, url)
                    continue

                availability = check_availability(url, headers)
                (job_id, res_content) = request_capture(url, headers)

                if not job_id:
                    update_values(service,
                                  spreadsheet_id,
                                  sheet + '!B' + str(row_index) + ':D' + str(row_index),
                                  [availability, res_content, ''])
                    error_count = error_count + 1
                    update_state(row_index, values, error_count, url)
                    continue

                (status, captured_url, success) = request_capture_status(job_id, headers)

                update_values(service,
                              spreadsheet_id,
                              sheet + '!B' + str(row_index) + ':D'+ str(row_index),
                              [availability, status, captured_url])

                if not success:
                    error_count = error_count + 1

                update_state(row_index, values, error_count, url)


def update_values(service, spreadsheet_id, range, values):
    body = {
        'values':[values]
    }

    service.spreadsheets().values().update(spreadsheetId=spreadsheet_id, valueInputOption='RAW',
                                       range=range, body=body).execute()

def update_state(row_index, values, error_count, url):
    if row_index - 2 == len(values):
        current_task.update_state(state='SUCCESS',
                                  meta={
                                      'percent': (row_index - 2) / len(values) * 100,
                                      'current': row_index - 2,
                                      'total': len(values),
                                      'error': error_count,
                                      'url': url
                                  })
        raise Ignore()
    else:
        current_task.update_state(state='PROGRESS',
                                  meta={
                                      'percent': (row_index - 2) / len(values) * 100,
                                      'current': row_index - 2,
                                      'total': len(values),
                                      'error': error_count,
                                      'url': url
                                  })

def is_valid_url(url):
    match = re.match(r'(ftp|http|https):\/\/(\w+:{0,1}\w*@)?(\S+)(:[0-9]+)?(\/|\/([\w#!:.?+=&%@!\-\/]))?', url)
    return match is not None

def request_capture(url, headers):
    response = requests.get(url=SPN_URL + url, headers=headers)

    try:
        data = response.json()
        return (data['job_id'], data)
    except:
        return (None, str(response.content))

def request_capture_status(job_id, headers):
    time.sleep(10)
    response = requests.get(
        url='%sstatus/%s?_t=%s' % (SPN_URL, job_id, str(time.time())),
        headers=headers
    )

    try:
        data = response.json()
        if data['status'] == 'pending':
            return request_capture_status(job_id, headers)
        else:
            if 'timestamp' in data and 'original_url' in data:
                return (data['status'], 'http://web.archive.org/web/' + data['timestamp'] + '/' + data['original_url'], True)
            else:
                return ('Error: ' + data['message'], '', False)
    except:
        return (str(response.content), '', False)

def check_availability(url, headers):
    response = requests.get(url=AVAILABILITY_API_URL + '?url=' + url, headers=headers)

    if get_wayback_url_from_response(response.json()):
        return True

    return False

def get_wayback_url_from_response(json):
    ret = None

    if (json and
        json['archived_snapshots'] and
        json['archived_snapshots']['closest'] and
        json['archived_snapshots']['closest']['available'] and
        json['archived_snapshots']['closest']['available'] == True and
        json['archived_snapshots']['closest']['status'] == '200' and
        is_valid_url(json['archived_snapshots']['closest']['url'])):

        ret = make_https(json['archived_snapshots']['closest']['url'])

    return ret

def make_https(url):
    return url.replace('http:', 'https:')