import operator
import os
import re
from datetime import timedelta, datetime

from six.moves.urllib.parse import urljoin

import requests
import lz4framed

from flask import Blueprint, render_template, current_app, request, redirect, \
    Response, stream_with_context
from pycountry import countries
from sqlalchemy import func
from sqlalchemy.orm.exc import NoResultFound, MultipleResultsFound
from werkzeug.exceptions import BadRequest, NotFound, HTTPException

from measurements.models import Report, Measurement, Autoclaved

pages_blueprint = Blueprint('pages', 'measurements',
                            static_folder='static',
                            static_url_path='/static/')


@pages_blueprint.route('/')
def index():
    return render_template('index.html')


@pages_blueprint.route('/stats')
def stats():
    return render_template('stats.html')


@pages_blueprint.route('/files')
def files_index():
    return render_template('files/index.html')


def _calendarized_count():
    DT_FRMT = '%Y-%m-%d'
    one_day = timedelta(days=1)

    q = current_app.db_session.query(
        func.count(func.date_trunc('day', Report.test_start_time)),
        func.date_trunc('day', Report.test_start_time)
    ).group_by(func.date_trunc('day', Report.test_start_time)).order_by(
        Report.test_start_time
    )
    if q.first() is None:
        raise StopIteration

    _, first_date = q.first()
    count_map = {}
    for count, day in q:
        count_map[day] = count
    last_date = day
    start = first_date

    # here we pad up the days to the first week
    pad_from = first_date - timedelta(days=first_date.weekday())
    current_month = pad_from.month
    week = []
    month = []
    while pad_from <= first_date:
        week.append([pad_from, -1])
        pad_from += one_day

    while start <= last_date:
        if start.month != current_month:
            current_month = start.month
            month.append(week)
            yield month
            month = []
            pad_from = start - timedelta(days=start.weekday())
            week = []
            while pad_from < start:
                week.append([pad_from, -2])
                pad_from += one_day

        count = count_map.get(start.strftime(DT_FRMT), 0)
        week.append([start, count])
        if len(week) == 7:
            month.append(week)
            week = []

        start += one_day

    while len(week) < 7:
        week.append([start, -1])
        start += one_day

    if len(week) > 0:
        month.append(week)

    yield month


def _report_dates():
    q = current_app.db_session.query(
        func.count(func.date_trunc('day', Report.test_start_time)),
        func.date_trunc('day', Report.test_start_time)
    ).group_by(func.date_trunc('day', Report.test_start_time)).order_by(func.date_trunc('day', Report.test_start_time))
    for row in q:
        count, day = row
        yield {
            'count': count,
            'date': day.strftime("%Y-%m-%d")
        }


@pages_blueprint.route('/files/by_date')
def files_by_date():
    view = request.args.get("view", "list")
    if view == "calendar":
        return render_template('files/by_date_calendar.html')
        # XXX this is actually not used
        # calendar_count=_calendarized_count())
    else:
        return render_template('files/by_date_list.html',
                               report_dates=_report_dates())


def _files_on_date(date, order_by, order):
    q = current_app.db_session.query(Report) \
            .filter(func.date_trunc('day', Report.test_start_time) == date) \
            .order_by("%s %s" % (order_by, order))
    return q


@pages_blueprint.route('/files/by_date/<date>')
def files_on_date(date):
    # XXX do some validation of date
    order_by = request.args.get('order_by', 'test_start_time')
    order = request.args.get('order', 'desc')
    if order.lower() not in ('desc', 'asc'):
        raise BadRequest()
    if order_by not in ('test_start_time', 'probe_cc', 'report_id',
                        'test_name', 'probe_asn'):
        raise BadRequest()
    return render_template('files/list.html',
                           report_files=_files_on_date(date,
                                                       order_by=order_by,
                                                       order=order),
                           by='date',
                           order=order,
                           order_by=order_by,
                           current_date=date)


def _files_by_country():
    results = []
    q = current_app.db_session.query(
        func.count(Report.probe_cc),
        Report.probe_cc
    ).group_by(Report.probe_cc).order_by(Report.probe_cc)
    for row in q:
        count, alpha_2 = row
        country = "Unknown"
        if alpha_2 != "ZZ":
            try:
                country = countries.get(alpha_2=alpha_2).name
            except KeyError:
                country = "Unknown (%s)" % alpha_2
        results.append({
            'count': count,
            'alpha2': alpha_2,
            'country': country
        })
    results.sort(key=operator.itemgetter('country'))
    return results


@pages_blueprint.route('/files/by_country')
def files_by_country():
    view = request.args.get("view", "list")
    if view == "flag":
        return render_template('files/by_country_flag.html',
                               report_countries=_files_by_country())
    else:
        return render_template('files/by_country_list.html',
                               report_countries=_files_by_country())


def _files_in_country(country_code, order_by, order):
    q = current_app.db_session.query(Report) \
            .filter(Report.probe_cc == country_code) \
            .order_by("%s %s" % (order_by, order))
    return q


@pages_blueprint.route('/files/by_country/<country_code>')
def files_in_country(country_code):
    # XXX do some validation of date
    order_by = request.args.get('order_by', 'test_start_time')
    order = request.args.get('order', 'desc')
    if order.lower() not in ('desc', 'asc'):
        raise BadRequest()
    if order_by not in ('test_start_time', 'probe_cc', 'report_id',
                        'test_name', 'probe_asn'):
        raise BadRequest()
    return render_template('files/list.html',
                           report_files=_files_in_country(
                               country_code, order_by=order_by,
                               order=order),
                           by='country',
                           order=order,
                           order_by=order_by,
                           current_country=country_code)

def decompress_autoclaved(autoclaved_filename, frames_off, frames_size, intra_off, intra_size):
    def generator():
        url = urljoin(current_app.config['AUTOCLAVED_BASE_URL'], autoclaved_filename)

        current_app.logger.debug("Fetching autoclaved from: %s" % url)
        current_app.logger.debug("  frames_off: %d" % frames_off)
        current_app.logger.debug("  frames_size: %d" % frames_size)
        current_app.logger.debug("  intra_off: %d" % intra_off)
        current_app.logger.debug("  intra_size: %d" % intra_size)
        try:
            r = requests.get(url, headers={"Range": "bytes={}-{}".format(frames_off, frames_off + frames_size)},
                                stream=True)
            streamed_data = 0
            for chunk in lz4framed.Decompressor(r.raw):
                current_app.logger.debug("chunking away 1")
                d = chunk
                current_app.logger.debug("chunking away 2")
                if streamed_data == 0 and intra_off > 0:
                    d = chunk[intra_off:]
                if streamed_data > (intra_size + len(d)):
                    d = chunk[:(streamed_data - intra_size)]
                yield d
                streamed_data += len(d)
            current_app.logger.debug("sent: %d" % streamed_data)
        except Exception as exc:
            current_app.logger.error("failed to fetch streamed data: %s" % exc)
            raise HTTPException("Failed to fetch data")
    return generator

@pages_blueprint.route('/files/download/<path:textname>')
def files_download(textname):
    q = current_app.db_session.query(
            Measurement.id.label('m_id'),
            Measurement.report_no.label('report_no'),
            Measurement.frame_off.label('frame_off'),
            Measurement.frame_size.label('frame_size'),
            Measurement.intra_off.label('intra_off'),
            Measurement.intra_size.label('intra_size'),
            Report.report_no.label('r_report_no'),
            Report.autoclaved_no.label('r_autoclaved_no'),
            Autoclaved.filename.label('a_filename'),
            Autoclaved.autoclaved_no.label('a_autoclaved_no'),
    ).filter(Report.textname == textname) \
        .join(Report, Report.report_no == Measurement.report_no) \
        .join(Autoclaved, Autoclaved.autoclaved_no == Report.autoclaved_no) \
        .order_by(Measurement.frame_off.asc(), Measurement.intra_off.asc())

    msmts = q.all()
    if len(msmts) == 0:
        current_app.logger.debug("Could not find %s" % textname)
        raise NotFound("No file with that filename found")

    autoclaved_filename = msmts[0].a_filename
    intra_off = msmts[0].intra_off
    intra_size = 0
    for msmt in msmts:
        if autoclaved_filename != msmt.a_filename:
            raise HTTPException("Autoclaved file is spanned across multiple files")
        intra_size += msmt.intra_size
    frames_off = msmts[0].frame_off
    frames_size = msmts[-1].frame_off - frames_off + msmts[-1].frame_size
    resp_generator = decompress_autoclaved(autoclaved_filename, frames_off, frames_size, intra_off, intra_size)
    return Response(stream_with_context(resp_generator()), mimetype='text/json')

# These two are needed to avoid breaking older URLs
DAY_REGEXP = re.compile("^\d{4}\-[0-1]\d\-[0-3]\d$")
@pages_blueprint.route('/<date>/<report_file>')
def backward_compatible_download(date, report_file):
    if DAY_REGEXP.match(date) and report_file.endswith(".json"):
        # XXX maybe do some extra validation on report_file
        return redirect('/files/download/%s' % report_file)
    raise NotFound


@pages_blueprint.route('/<date>')
def backward_compatible_by_date(date):
    if DAY_REGEXP.match(date):
        return redirect('/files/by_date/%s' % date)
    raise NotFound