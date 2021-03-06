from abc import ABCMeta, abstractmethod

from fluent import sender

import re
import os
import time
import configparser
import numpy as np

from .datadog_client import DatadogClient
from .changefinder.ar_1d import ModelSelection
from .changefinder.changefinder_1d import ChangeFinder

from logging import getLogger
logger = getLogger('ChangeFinder')


class Detector:

    __metaclass__ = ABCMeta

    @abstractmethod
    def __init__(self, fluent_tag_prefix, inifile_path):
        self.fluent_logger = sender.FluentSender(fluent_tag_prefix)
        self.inifile_path = inifile_path

        self.dd = DatadogClient(app_key=os.environ['DD_APP_KEY'],
                                api_key=os.environ['DD_API_KEY'])

        # key: config's section_name
        # value: { query: (query string), cf: (ChangeFinder instance) }
        self.dd_sections = {}
        self.load_dd_config()

    def select_k(self, query):
        end = int(time.time())
        start = end - (60 * 60 * 24)  # one day interval

        series = self.dd.get_series(start, end, query)

        x = np.array([(0.0 if s['raw_value'] is None else s['raw_value']) for s in series])
        return ModelSelection().select(x)[0]

    def load_dd_config(self):
        parser = configparser.ConfigParser()
        parser.read(self.inifile_path)

        dd_section_names = [s for s in parser.sections()
                            if re.match('^datadog\..*$', s) is not None]

        # delete previously existed, but now deleted sections
        for section_name in (set(self.dd_sections.keys()) - set(dd_section_names)):
            del self.dd_sections[section_name]

        # create ChangeFinder instances for each query (metric)
        for section_name in dd_section_names:
            # since this method can be called multiple times,
            # only new DD-related sections are handled
            if section_name in self.dd_sections.keys():
                continue

            self.dd_sections[section_name] = {}

            s = parser[section_name]

            q = s.get('query')
            self.dd_sections[section_name]['query'] = q

            r = s.getfloat('r') or 0.02
            T1 = s.getint('T1') or 10
            T2 = s.getint('T2') or 5

            k = s.getint('k')
            if k is None:
                k = self.select_k(q)
                logger.info('[%s] `k` has been automatically set to %d' % (section_name, k))

            self.dd_sections[section_name]['cf'] = ChangeFinder(r=r, k=k, T1=T1, T2=T2)

    def query(self, start, end):
        for section_name in self.dd_sections.keys():
            series = self.dd.get_series(start, end,
                                        self.dd_sections[section_name]['query'])

            self.__handle_series(section_name, series)

    def __handle_series(self, section_name, series):
        start = time.clock()

        for s in series:
            s['raw_value'] = 0.0 if s['raw_value'] is None else s['raw_value']
            score_outlier, score_change = self.dd_sections[section_name]['cf'].update(s['raw_value'])

            s['dst_metric'] = re.match('^datadog\.(.*)$', section_name).group(1)

            record = self.__get_record(s, score_outlier, score_change)

            if not self.fluent_logger.emit(s['dst_metric'], record):
                err = str(self.fluent_logger.last_error)
                logger.error('fluent-logger failed to emit records for [%s] (%s)' % (section_name, err))
                self.fluent_logger.clear_last_error()
                break

        logger.info('Finish emitting %d records for [%s] (%.3f sec.)' % (len(series), section_name, time.clock() - start))

    def __get_record(self, s, score_outlier, score_change):
        return {'metric': s['src_metric'],
                'raw_value': s['raw_value'],
                'metric_outlier': 'changefinder.outlier.' + s['dst_metric'],
                'score_outlier': score_outlier,
                'metric_change': 'changefinder.change.' + s['dst_metric'],
                'score_change': score_change,
                'scope': s['scope'],
                'host': s['host'],
                'time': int(s['time'] / 1000)}  # same as Ruby's unix time
