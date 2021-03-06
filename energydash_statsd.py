#!/usr/bin/env python

################################################################################
# file:        energydash_statsd.py
# description: energydash stats update daemon
################################################################################
# Copyright 2013 Chris Linstid
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
################################################################################

from pymongo import MongoClient
import pymongo
import logging
from time import sleep
from datetime import datetime, timedelta
import pytz
import urllib

from settings import *
from utc_conversion import *

def update_average(old_average, old_count, new_value):
    '''
    Update an existing average using the old average, the old count, and the
    new value. The result is returned in a tuple: (new_average, new_count)
    '''
    new_count = old_count + 1
    new_average = ((old_average * old_count) + new_value) / new_count
    return (new_average, new_count)

class Stats(object):
    def __init__(self):
        self.stopping = False
        mongo_uri = 'mongodb://{user}:{password}@{host}/{database}'.format(user=urllib.quote(MONGO_USER),
                                                                           password=urllib.quote(MONGO_PASSWORD),
                                                                           host=MONGO_HOST,
                                                                           database=MONGO_DATABASE_NAME)
        connected = False
        while not connected:
            try:
                self.client = MongoClient(MONGO_HOST, replicaset=MONGO_REPLICA_SET)
                connected = True
            except pymongo.errors.ConnectionFailure as e:
                logger.info('Failed to connect to mongodb [{}], retrying.'.format(e))
                sleep(1)

        self.db = self.client[MONGO_DATABASE_NAME]
        self.logger = logging.getLogger('Stats')

    def stop(self):
        self.client.disconnect()
        self.stopping = True

    def update_minutes_and_hours_from_readings(self):
        '''
        This method updates the ten_minutes and hours collections with
        documents that contain the average usage and temp_f for each
        granularity for all of the data available from the envir_reading
        collection.

        Collections used:
            1. envir_reading: Collection of readings from energy monitor
               receiver every 6 seconds.

            2. ten_minutes: Collection of usage and temp_f averages for each 10
               minute window over all of the collected data.

            2. hours: Collection of usage and temp_f averages for each hour
               over all of the collected data.
        '''

        readings = self.db.envir_reading
        hours = self.db.hours
        ten_minutes = self.db.ten_minutes
        bookmarks = self.db.bookmarks
        logger = self.logger

        logger.info('Updating minutes/hours from envir_reading.')

        # Figure out where we left off by finding the timestamp of the last
        # bookmark.
        query = {}
        reading_bookmark = bookmarks.find_one({'_id': 'envir_reading'})
        if reading_bookmark is not None:
            logger.info('Last bookmark was {}'.format(reading_bookmark))
            query = {
                'reading_timestamp': {
                     '$gt': reading_bookmark['timestamp']
                 }
            }

        logger.info('{} total readings.'.format(readings.count()))

        index_ensured = False;
        while not index_ensured:
            try:
                readings.ensure_index('reading_timestamp', pymongo.ASCENDING)
                index_ensured = True;
            except Exception as e:
                logger.info('Failed to ensure index ({}), retrying.'.format(e))
                sleep(1)


        cursor = readings.find(query).sort('reading_timestamp', pymongo.ASCENDING)
        logger.info('{} new readings since last bookmark.'.format(cursor.count()));

        current_hour = None
        reading_count = 0
        for reading in cursor:
            reading_count += 1
            if (reading_count % 1000) == 0:
                logger.info('Processed {} readings.'.format(reading_count))

            if reading['total_watts'] == 0 or reading['temp_f'] == 0:
                logger.info('Skipping reading at {}: total_watts({}) temp_f({})'.format(
                            reading['reading_timestamp'], reading['total_watts'], reading['temp_f']))
                continue

            timestamp = reading['reading_timestamp']
            if not current_hour or timestamp >= (current_hour['_id'] + timedelta(hours=1)):
                if current_hour:
                    saved = False
                    while not saved:
                        try:
                            new_id = hours.save(current_hour)
                            saved = True
                        except Exception as e:
                            logger.error('Failed to save current hour: {}'.format(e))
                            sleep(1)

                    logger.info("Moving to new hour {}".format(new_id))

                current_hour_start = datetime(year=timestamp.year,
                                              month=timestamp.month,
                                              day=timestamp.day,
                                              hour=timestamp.hour)
                logger.debug('Looking for hour at {}'.format(current_hour_start))
                current_hour = hours.find_one({'_id': current_hour_start})

                if current_hour is None:
                    current_hour = {
                                    '_id': current_hour_start,
                                    'count': 0,
                                    'average_usage': 0,
                                    'average_tempf': 0,
                                    'timestamps': []
                                   }
                    logger.debug('Creating hour document: {}'.format(current_hour_start))

            if timestamp not in current_hour['timestamps']:
                (current_hour['average_usage'], temp_count) = update_average(
                    old_average=current_hour['average_usage'],
                    old_count=current_hour['count'],
                    new_value=reading['total_watts'])
                (current_hour['average_tempf'], current_hour['count']) = update_average(
                    old_average=current_hour['average_tempf'],
                    old_count=current_hour['count'],
                    new_value=reading['temp_f'])

                current_hour['timestamps'].append(timestamp)

            if reading_bookmark is None:
                reading_bookmark = {
                    '_id': 'envir_reading',
                    'timestamp': timestamp
                }
            else:
                reading_bookmark['timestamp'] = timestamp

        if current_hour:
            saved = False
            while not saved:
                try:
                    new_id = hours.save(current_hour)
                    saved = True
                except Exception as e:
                    logger.error('Failed to save current hour: {}'.format(e))
                    sleep(1)

            logger.debug('Saved hour document {}'.format(new_id))
        if reading_bookmark:
            saved = False
            while not saved:
                try:
                    bookmarks.save(reading_bookmark)
                    saved = True
                except Exception as e:
                    logger.error('Failed to save reading bookmark: {}'.format(e))
                    sleep(1)

            logger.info('Saved bookmark at {}.'.format(reading_bookmark))

    def update_hours_per_day_from_hours(self):
        '''
        This method updates two different collections:
            1. Update averages for each hour in any day, so we should have 24
               documents per collection that we updated.

            2. Update averages for each hour for each day of the week, so we
               should have 7 documents with 24 averages per day (both usage and
               temp_f).

        Collections used:
            1. hours: Collection of usage and temp_f averages for each hour
               over all of the collected data.

            2. hours_per_dow: Collection of usage and temp_f averages for each
               hour of each day of the week.

            3. hours_in_day: Collection of usage and temp_f averages for each
               hour in any day.
        '''
        logger = self.logger
        hours = self.db.hours
        hours_per_dow = self.db.hours_per_dow
        hours_in_day = self.db.hours_in_day
        bookmarks = self.db.bookmarks

        logger.info('Updating hours per day/dow from hours collection.')

        # We use a single bookmark for both hours per day of week and hours in
        # day because we're building those off of the same collection of hours.
        hour_bookmark = bookmarks.find_one({'_id': 'hours'})
        if hour_bookmark is None:
            hour_bookmark = {
                             '_id': 'hours',
                             'timestamp': epoch
                            }

        current_dow = None
        current_hour_of_day = None
        hours_cache = {}
        days_cache = {}
        local_tz = pytz.timezone(LOCAL_TIMEZONE)

        # We want $gte for finding our bookmark because we may have more values
        # in the last hour we processed that weren't there when we processed
        # them last.
        cursor = hours.find({'_id': {'$gte': hour_bookmark['timestamp']}})
        for hour in cursor:
            # We use a localized datetime for building these collections
            # because the day of the week values are really only relevant to
            # the timezone where the data is being collected. The hours in a
            # day don't really matter because they could be shifted by the
            # timezone offset of the browser, but again, the hours make more
            # sense when they're in the timezone of the collector. On top of
            # that, because we're localizing, we can compensate for DST since
            # we have the date.
            local_timestamp = pytz.utc.localize(hour['_id']).astimezone(local_tz)
            current_hour_num = str(local_timestamp.hour)
            logging.debug('Updating hour {}.'.format(current_hour_num))

            # Find the current_hour_of_day document to update.
            #
            # First check the current hour of day to see if it's what we want.
            if not current_hour_of_day or current_hour_of_day['_id'] != current_hour_num:
                # Next check in the hours cache
                if current_hour_num in hours_cache:
                    current_hour_of_day = hours_cache[current_hour_num]
                else:
                    # Next we go to the DB
                    current_hour_of_day = hours_in_day.find_one({'_id': current_hour_num})

                    if not current_hour_of_day:
                        # Not in the DB either, so we need to create a new document.
                        current_hour_of_day = {
                                               '_id': current_hour_num,
                                               'timezone': local_tz.zone,
                                               'average_usage': 0,
                                               'average_tempf': 0,
                                               'count': 0,
                                               'timestamps': []
                                              }

                    # Add the document (either what we pulled from the DB or
                    # just created) to the cache. We'll update the DB later
                    # when we're done processing the hours documents that we
                    # pulled from the DB.
                    hours_cache[current_hour_num] = current_hour_of_day

            if not hour['_id'] in current_hour_of_day['timestamps']:
                # Update the usage average.
                (current_hour_of_day['average_usage'],
                 temp_count) = update_average(
                                              old_average=current_hour_of_day['average_usage'],
                                              old_count=current_hour_of_day['count'],
                                              new_value=hour['average_usage'])

                # Update the tempf average.
                (current_hour_of_day['average_tempf'],
                 current_hour_of_day['count']) = update_average(
                    old_average=current_hour_of_day['average_tempf'],
                    old_count=current_hour_of_day['count'],
                    new_value=hour['average_tempf'])

                current_hour_of_day['timestamps'].append(hour['_id'])

            # Find the current day of the week document to update. These are
            # indexed by the abbreviated day name (probably could have used day
            # of week number, but the day name is easier to read and can be
            # passed straight to whoever asks for it).
            day_name = local_timestamp.strftime('%a')
            logging.debug('Updating {}.'.format(day_name))
            # Same dance as the hours, check if the current dow is the right one.
            if not current_dow or current_dow['_id'] != day_name:
                # Check the days cache next.
                if day_name in days_cache:
                    current_dow = days_cache[day_name]
                else:
                    # Check the DB
                    current_dow = hours_per_dow.find_one({'_id': day_name})

                    if not current_dow:
                        # Create a new document.
                        current_dow = {
                                       '_id': day_name,
                                       'timezone': local_tz.zone,
                                       'hours': {},
                                      }

                    # Add the new/pulled from DB document to the cache.
                    days_cache[day_name] = current_dow

            if current_hour_num in current_dow['hours']:
                current_hour_of_dow = current_dow['hours'][current_hour_num]
            else:
                current_hour_of_dow = {
                                       'average_usage': 0,
                                       'average_tempf': 0,
                                       'count': 0,
                                       'timestamps': []
                                       }
                current_dow['hours'][current_hour_num] = current_hour_of_dow

            if not hour['_id'] in current_hour_of_dow['timestamps']:
                # Update the usage average.
                (current_hour_of_dow['average_usage'], temp_count) = update_average(
                    old_average=current_hour_of_dow['average_usage'],
                    old_count=current_hour_of_dow['count'],
                    new_value=hour['average_usage'])

                # Update the tempf average.
                (current_hour_of_dow['average_tempf'], current_hour_of_dow['count']) = update_average(
                    old_average=current_hour_of_dow['average_tempf'],
                    old_count=current_hour_of_dow['count'],
                    new_value=hour['average_tempf'])

                current_hour_of_dow['timestamps'].append(hour['_id'])

            # Update the naive (UTC) datetime bookmark
            hour_bookmark['timestamp'] = hour['_id']

        # Save our updates to the DB
        for hour, doc in hours_cache.iteritems():
            saved = False
            while not saved:
                try:
                    hours_in_day.save(doc)
                    saved = True
                except Exception as e:
                    logger.error('Failed to save hours in day: {}'.format(e))
                    sleep(1)

            logger.info('Saved hour {}.'.format(hour))

        for day, doc in days_cache.iteritems():
            saved = False
            while not saved:
                try:
                    hours_per_dow.save(doc)
                    saved = True
                except Exception as e:
                    logger.error('Failed to save day of week: {}'.format(e))
                    sleep(1)

            logger.info('Saved day {}.'.format(day))

        # Save the last bookmark we processed.
        saved = False
        while not saved:
            try:
                bookmarks.save(hour_bookmark)
                saved = True
            except Exception as e:
                logger.error('Failed to save hour bookmark: {}'.format(e))
                sleep(1)

        logger.info('Saved bookmark {}'.format(hour_bookmark))

    def update_stats(self):
        self.update_minutes_and_hours_from_readings()
        self.update_hours_per_day_from_hours()

    def run(self):
        while not self.stopping:
            self.update_stats()
            sleep(60)

def main():
    logging.basicConfig(format='[%(asctime)s/%(name)s]: %(message)s',
                        level=logging.INFO)
    logger = logging.getLogger('main')
    if DEBUG:
        logger.setLevel(logging.DEBUG)

    stats = Stats()
    while not stats.stopping:
        try:
            stats.run()
        except KeyboardInterrupt:
            logger.info('Ctrl-C received, exiting.')
            stats.stop()
        except Exception as e:
            logger.error('Caught unhandled exception: {}'.format(e))
            stats.stop()
            raise

    logger.info('Exiting.')

if __name__ == '__main__':
    main()
