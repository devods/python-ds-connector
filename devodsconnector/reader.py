import os
import sys
import configparser
import datetime
from datetime import timezone
import json
import csv
import warnings
from pathlib import Path
from collections import namedtuple, defaultdict
import numpy as np
import pandas as pd
from scipy.stats import norm

from devo.api import Client

from .error_checking import check_status

csv.field_size_limit(sys.maxsize)
warnings.simplefilter('always', UserWarning)


class Reader(object):

    def __init__(self, profile='default', api_key=None, api_secret=None,
                       end_point=None, oauth_token=None, jwt=None,
                       credential_path=None):

        self.profile = profile
        self.api_key = api_key
        self.api_secret = api_secret
        self.end_point = end_point
        self.oauth_token = oauth_token
        self.jwt = jwt

        if credential_path is None:
                self.credential_path = Path.home() / '.devo_credentials'
        else:
            self.credential_path = Path(credential_path).resolve().expanduser()

        if not (self.end_point and (self.oauth_token or self.jwt or (self.api_key and self.api_secret))):
            self._read_profile()

        if not (self.end_point and (self.oauth_token or self.jwt or (self.api_key and self.api_secret))):
            raise Exception('End point and either API keys or OAuth Token must be specified or in ~/.devo_credentials')

        self.client = Client(auth=dict(key=self.api_key,secret=self.api_secret,
                                       token=self.oauth_token,jwt=self.jwt),
                             address=self.end_point)

    def _read_profile(self):
        """
        Read Devo API keys from a credentials file located
        at ~/.devo_credentials if credentials are not provided

        Use profile to specify which set of credentials to use
        """

        config = configparser.ConfigParser()
        config.read(self.credential_path)

        if self.profile in config:
            profile_config = config[self.profile]

            self.api_key = profile_config.get('api_key')
            self.api_secret = profile_config.get('api_secret')
            self.end_point = profile_config.get('end_point')
            self.oauth_token = profile_config.get('oauth_token')

            if self.end_point == 'USA':
                self.end_point = 'https://apiv2-us.devo.com/search/query'
            elif self.end_point == 'EU':
                self.end_point = 'https://apiv2-eu.devo.com/search/query'

    def query(self, linq_query, start, stop=None, output='dict', ts_format='datetime'):

        valid_outputs = ('dict', 'list', 'namedtuple', 'dataframe')
        if output not in valid_outputs:
            raise Exception(f"Output must be one of {valid_outputs}")

        if output=='dataframe' and stop is None:
            raise Exception("DataFrame can't be build from continuous query")

        results = self._stream(linq_query,start,stop,ts_format)
        cols = next(results)

        return getattr(self, f'_to_{output}')(results,cols)

    def _stream(self, linq_query, start, stop, ts_format):
        """
        yields columns names then rows in lists with converted
        types
        """

        type_dict = self._get_types(linq_query, start, ts_format)

        result = self._query(linq_query, start, stop, mode = 'csv', stream = True)
        result = self._decode_results(result)
        result = csv.reader(result)

        cols = next(result)

        if len(cols) != len(type_dict):
            raise Exception("Duplicate column names encountered, custom columns must be named")

        type_list = [type_dict[c] for c in cols]

        yield cols

        for row in result:
            yield [t(v) for t, v in zip(type_list, row)]

    def _query(self, linq_query, start, stop=None, mode='csv', stream=False, limit=None):

        start = self._to_unix(start)
        stop = self._to_unix(stop)

        dates = {'from': start, 'to':stop}
        self.client.config.response = mode
        self.client.config.stream = stream

        response = self.client.query(query=linq_query,
                                     dates=dates,
                                     limit=limit)

        return response


    @staticmethod
    def _null_decorator(f):
        def null_f(v):
            if v == '':
                return None
            else:
                return f(v)
        return null_f

    @staticmethod
    def make_ts_func(ts_format):
        if ts_format not in ('datetime', 'iso', 'timestamp'):
            raise Exception('ts_format must be one of: datetime, iso, or timestamp ')

        def ts_func(t):
            dt = datetime.datetime.strptime(t.strip(), '%Y-%m-%d %H:%M:%S.%f')

            if ts_format == 'datetime':
                return dt
            elif ts_format == 'iso':
                return dt.isoformat()
            elif ts_format == 'timestamp':
                return dt.timestamp()
            else:
                raise Exception('ts_format must be one of: datetime, iso, or timestamp ')
        return ts_func

    def _make_type_map(self,ts_format):

        funcs = {
                'timestamp': self.make_ts_func(ts_format),
                'str': str,
                'int8': int,
                'int4': int,
                'float8': float,
                'float4': float,
                'bool': lambda b: b == 'true'
               }

        decorated_funcs = {t: self._null_decorator(f) for t, f in funcs.items()}
        decorated_str = self._null_decorator(str)

        return defaultdict(lambda: decorated_str, decorated_funcs)

    def _get_types(self,linq_query,start,ts_format):
        """
        Gets types of each column of submitted
        """
        type_map = self._make_type_map(ts_format)

        # so we don't have  stop ts in future as required by API V2
        stop = self._to_unix(start)
        start = stop - 1

        response = self._query(linq_query, start=start, stop=stop, mode='json/compact', limit=1)

        try:
            data = json.loads(response)
            check_status(data)
        except ValueError:
            raise Exception('API V2 response error')

        col_data = data['object']['m']
        type_dict = { c:type_map[v['type']] for c,v in col_data.items() }

        return type_dict

    @staticmethod
    def _to_unix(date, milliseconds=False):
        """
        Convert date to a unix timestamp

        date: A unix timestamp in second, a datetime object,
        pandas.Timestamp object, or string to be parsed
        by pandas.to_datetime
        """

        if date is None:
            return None

        elif date == 'now':
            epoch = datetime.datetime.now().timestamp()
        elif type(date) == str:
            epoch = pd.to_datetime(date).timestamp()
        elif isinstance(date, (pd.Timestamp, datetime.datetime)):
            epoch = date.replace(tzinfo=timezone.utc).timestamp()
        elif isinstance(date, (int,float)):
            epoch = date
        else:
            raise Exception('Invalid Date')

        if milliseconds:
            epoch *= 1000

        return int(epoch)

    @staticmethod
    def _decode_results(r):
        r = iter(r)

        # catch error not reported for json/compact
        first = next(r)
        try:
            data = json.loads(first)
            check_status(data)
        except ValueError:
            pass

        yield first.decode('utf-8').strip()  # APIV2 adding space to first line of aggregates
        for l in r:
            yield l.decode('utf-8')

    @staticmethod
    def _to_list(results,cols):
        yield from results

    @staticmethod
    def _to_dict(results, cols):
        for row in results:
            yield {c:v for c,v in zip(cols,row)}

    @staticmethod
    def _to_namedtuple(results, cols):
        Row = namedtuple('Row', cols)
        for row in results:
            yield Row(*row)

    @staticmethod
    def _to_dataframe(results,cols):
        return pd.DataFrame(results, columns=cols).fillna(np.nan)

    def randomSample(self,linq_query,start,stop,sample_size):

        if (sample_size < 1) or (not isinstance(sample_size, int)):
            raise Exception('Sample size must be a positive int')

        size_query = f'{linq_query} group select count() as count'

        r = self.query(size_query,start,stop,output='list')
        table_size = next(r)[0]

        if sample_size >= table_size:
            warning_msg = 'Sample size greater than or equal to total table size. Returning full table'
            warnings.warn(warning_msg)
            return self.query(linq_query,start,stop,output='dataframe')

        p = self._find_optimal_p(n=table_size,k=sample_size,threshold=0.99)

        sample_query = f'{linq_query} where simplify(float8(rand())) < {p}'

        while True:
            df = self.query(sample_query,start,stop,output='dataframe')

            if df.shape[0] >= sample_size:
                return df.sample(sample_size).sort_index().reset_index(drop=True)
            else:
                pass

    @staticmethod
    def _loc_scale(n,p):
        """
        Takes parameters of a binomial
        distribution and finds the mean
        and std for a normal approximation

        :param n: number of trials
        :param p: probability of success
        :return: mean, std
        """
        loc = n*p
        scale = np.sqrt(n*p*(1-p))

        return loc,scale

    def _find_optimal_p(self,n,k,threshold):
        """
        Use a normal approximation to the
        binomial distribution.  Starts with
        p such that mean of B(n,p) = k
        and iterates.

        :param n: number of trials
        :param k: desired number of successes
        :param threshold: desired probability to achieve k successes

        :return: probability that a single trial that will yield
                 at least k success with n trials with probability of threshold

        """
        p = k / n
        while True:
            loc, scale = self._loc_scale(n, p)
            # sf = 1 - cdf, but can be more accurate according to scipy docs
            if norm.sf(x=k - 0.5, loc=loc, scale=scale) > threshold:
                break
            else:
                p = min(1.001*p, 1)

        return p

    def population_sample(self, query, start, stop, column, sample_size):

        population_query = f'{query} group by {column}'

        df = self.randomSample(population_query, start, stop, sample_size)
        population = df[column]
        sample_set = ','.join(f'"{x}"' for x in population)

        sample_query = f'{query} where str({column}) in {{{sample_set}}}'

        return self.query(sample_query, start, stop, output='dataframe')
