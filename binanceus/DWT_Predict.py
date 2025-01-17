from datetime import datetime
from functools import reduce

import numpy as np
# Get rid of pandas warnings during backtesting
import pandas as pd
from pandas import DataFrame, Series


from sklearn.ensemble import GradientBoostingRegressor
from sklearn.preprocessing import RobustScaler
from xgboost import XGBRegressor

import freqtrade.vendor.qtpylib.indicators as qtpylib

from freqtrade.strategy import (IStrategy, DecimalParameter, CategoricalParameter)

pd.options.mode.chained_assignment = None  # default='warn'

# Strategy specific imports, files must reside in same folder as strategy
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent))

import logging
import warnings

log = logging.getLogger(__name__)
# log.setLevel(logging.DEBUG)
warnings.simplefilter(action='ignore', category=pd.errors.PerformanceWarning)


from DataframeUtils import DataframeUtils, ScalerType
import pywt
import talib.abstract as ta

"""
####################################################################################
DWT_Predict - use a Discreet Wavelet Transform to model the price, and an sklearn
              regression algorithm trained on the DWT coefficients, which is then used
              to predict future prices.
              Unfortunately, this must all be done in a rolling fashion to avoid lookahead
              bias - so it is pretty slow

####################################################################################
"""


class DWT_Predict(IStrategy):
    # Do *not* hyperopt for the roi and stoploss spaces

    # ROI table:
    minimal_roi = {
        "0": 0.06
    }

    # Stoploss:
    stoploss = -0.10

    # Trailing stop:
    trailing_stop = False
    trailing_stop_positive = None
    trailing_stop_positive_offset = 0.0
    trailing_only_offset_is_reached = False

    timeframe = '5m'
    inf_timeframe = '15m'

    use_custom_stoploss = True

    # Recommended
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = True

    # Required
    startup_candle_count: int = 128  # must be power of 2
    win_size = 14

    process_only_new_candles = True

    custom_trade_info = {}

    ###################################

    # Strategy Specific Variable Storage

    ## Hyperopt Variables

    dwt_window = startup_candle_count

    lookahead = 6

    df_coeffs: DataFrame = None
    coeff_model = None
    dataframeUtils = None
    scaler = RobustScaler()

    # DWT  hyperparams
    entry_dwt_diff = DecimalParameter(0.0, 5.0, decimals=1, default=0.4, space='buy', load=True, optimize=True)
    exit_dwt_diff = DecimalParameter(-5.0, 0.0, decimals=1, default=-0.3, space='sell', load=True, optimize=True)

    # trailing stoploss
    tstop_start = DecimalParameter(0.0, 0.06, default=0.019, decimals=3, space='sell', load=True, optimize=True)
    tstop_ratio = DecimalParameter(0.7, 0.99, default=0.8, decimals=3, space='sell', load=True, optimize=True)

    # profit threshold exit
    profit_threshold = DecimalParameter(0.005, 0.065, default=0.06, decimals=3, space='sell', load=True, optimize=True)
    use_profit_threshold = CategoricalParameter([True, False], default=True, space='sell', load=True, optimize=False)

    # loss threshold exit
    loss_threshold = DecimalParameter(-0.065, -0.005, default=-0.046, decimals=3, space='sell', load=True, optimize=True)
    use_loss_threshold = CategoricalParameter([True, False], default=True, space='sell', load=True, optimize=False)

    # use exit signal? Use this for hyperopt, but once decided it's better to just set self.ignore_exit_signals
    enable_exit_signal = CategoricalParameter([True, False], default=True, space='sell', load=True, optimize=False)



    plot_config = {
        'main_plot': {
            'close': {'color': 'cornflowerblue'},
            # 'dwt_model': {'color': 'lightsalmon'},
            'dwt_predict': {'color': 'mediumaquamarine'},
        },
        'subplots': {
            "Diff": {
                'model_diff': {'color': 'brown'},
            },
        }
    }

    ###################################

    """
    Informative Pair Definitions
    """

    def informative_pairs(self):
        return []

    ###################################

    """
    Indicator Definitions
    """

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        # Base pair dataframe timeframe indicators
        curr_pair = metadata['pair']

        print("")
        print(curr_pair)
        print("")

        if self.dataframeUtils is None:
            self.dataframeUtils = DataframeUtils()
            self.dataframeUtils.set_scaler_type(ScalerType.Robust)

        # # build the DWT
        # print("    Building DWT...")
        # dataframe['dwt_model'] = dataframe['close'].rolling(window=self.dwt_window).apply(self.model)

        # RSI
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=self.win_size)

        # Williams %R
        dataframe['wr'] = 0.02 * (self.williams_r(dataframe, period=self.win_size) + 50.0)

        # Fisher RSI
        rsi = 0.1 * (dataframe['rsi'] - 50)
        dataframe['fisher_rsi'] = (np.exp(2 * rsi) - 1) / (np.exp(2 * rsi) + 1)

        # Combined Fisher RSI and Williams %R
        dataframe['fisher_wr'] = (dataframe['wr'] + dataframe['fisher_rsi']) / 2.0

        # build the list of model coefficients - added to self.df_coeffs
        print("    Building coefficients dataframe...")
        self.df_coeffs = None # reset for each pair
        coeffs = dataframe['close'].rolling(window=self.dwt_window).apply(self.add_coeffs)
        # dataframe = self.dataframe_add_coeff(dataframe)

        # print("    Merging coefficients into dataframe...")
        dataframe = self.merge_data(dataframe)

        print("    Training Model...")
        dataframe['dwt_predict'] = dataframe['close']
        self.train_model(dataframe)

        # add the predictions
        print("    Making predictions...")
        if self.dp.runmode.value not in ('hyperopt', 'backtest', 'plot'):
            dataframe = self.add_predictions(dataframe)
        else:
            dataframe = self.add_rolling_predictions(dataframe)

        dataframe['model_diff'] = 100.0 * (dataframe['dwt_predict'] - dataframe['close']) / dataframe[
            'close']

        return dataframe

    ###################################

    # Williams %R
    def williams_r(self, dataframe: DataFrame, period: int = 14) -> Series:
        """Williams %R, or just %R, is a technical analysis oscillator showing the current closing price in relation to the high and low
            of the past N days (for a given N). It was developed by a publisher and promoter of trading materials, Larry Williams.
            Its purpose is to tell whether a stock or commodity market is trading near the high or the low, or somewhere in between,
            of its recent trading range.
            The oscillator is on a negative scale, from −100 (lowest) up to 0 (highest).
        """

        highest_high = dataframe["high"].rolling(center=False, window=period).max()
        lowest_low = dataframe["low"].rolling(center=False, window=period).min()

        WR = Series(
            (highest_high - dataframe["close"]) / (highest_high - lowest_low),
            name=f"{period} Williams %R",
        )

        return WR * -100


    def madev(self, d, axis=None):
        """ Mean absolute deviation of a signal """
        return np.mean(np.absolute(d - np.mean(d, axis)), axis)

    def dwtModel(self, data):

        # the choice of wavelet makes a big difference
        # for an overview, check out: https://www.kaggle.com/theoviel/denoising-with-direct-wavelet-transform
        # wavelet = 'db1'
        # wavelet = 'bior1.1'
        wavelet = 'haar'  # deals well with harsh transitions
        level = 2
        wmode = "smooth"
        length = len(data)

        coeff = pywt.wavedec(data, wavelet, mode=wmode)

        # remove higher harmonics
        sigma = (1 / 0.6745) * self.madev(coeff[-level])
        uthresh = sigma * np.sqrt(2 * np.log(length))
        coeff[1:] = (pywt.threshold(i, value=uthresh, mode='hard') for i in coeff[1:])

        # inverse transform
        model = pywt.waverec(coeff, wavelet, mode=wmode)

        return model

    def model(self, a: np.ndarray) -> np.float:
        # must return scalar, so just calculate prediction and take last value
        # model = self.dwtModel(np.array(a))

        # de-trend the data
        w_mean = a.mean()
        w_std = a.std()
        x_notrend = (a - w_mean) / w_std

        # get DWT model of data
        restored_sig = self.dwtModel(x_notrend)

        # re-trend
        model = (restored_sig * w_std) + w_mean

        length = len(model)
        return model[length - 1]

    # function to get dwt coefficients
    def get_coeffs(self, data: np.array) -> np.array:

        length = len(data)

        # print(pywt.wavelist(kind='discrete'))

        # get the DWT coefficients
        # wavelet = 'haar'
        # level = 2
        # coeffs = pywt.wavedec(data, wavelet, level=level, mode='smooth')
        wavelet = 'db12'
        level = 2
        coeffs = pywt.wavedec(data, wavelet, mode='smooth')


        # remove higher harmonics
        sigma = (1 / 0.6745) * self.madev(coeffs[-level])
        uthresh = sigma * np.sqrt(2 * np.log(length))
        coeffs[1:] = (pywt.threshold(i, value=uthresh, mode='hard') for i in coeffs[1:])

        # flatten the coefficient arrays
        features = np.concatenate(np.array(coeffs, dtype=object))

        return features

    # adds coefficients to dataframe row
    # TODO: need to speed this up somehow
    def add_coeffs(self, a: np.ndarray) -> np.float:

        # get the DWT coefficients
        features = self.get_coeffs(np.array(a))

        # print("")
        # print(f"features: {np.shape(features)}")
        # print(features)
        # print("")

        if self.df_coeffs is None:
            # add headers (required by pandas because we want to merge later)
            cols = []
            for i in range(len(features)):
                col = "coeff_" + str(i)
                cols.append(col)
            self.df_coeffs = pd.DataFrame(columns=cols)

            # add rows of zeros to account fpr the rolling window startup
            zeros = []
            for i in range(self.dwt_window-1):
                zeros.append([0] * len(self.df_coeffs.columns))

            # Add the rows of zeros to the dataframe
            self.df_coeffs = pd.concat([self.df_coeffs, pd.DataFrame(zeros, columns=self.df_coeffs.columns)])

        # Append the coefficients to the df_coeffs dataframe
        self.df_coeffs.loc[len(self.df_coeffs)] = features

        return 1.0 # have to return a float value

    '''

    # adds coefficients to the dataframe. This is an attempt to speed up the rolling window processing of add_coeffs
    def dataframe_add_coeff(self, dataframe: DataFrame) -> DataFrame:

        close_data = np.array(dataframe['close'])

        init_df = False if "coeff_0" in dataframe.columns else True

        # roll through the close data and create DWT coefficients for each step
        nrows = len(close_data)
        start = 0
        coeff_array = []
        col_names  = []
        num_coeffs = 0

        for i in range(nrows):
            end = start + self.dwt_window - 1
            slice = close_data[start:end]

            features = self.get_coeffs(slice)

            # if not present, create columns in dataframe
            if init_df:
                init_df = False
                # add empty columns to dataframe
                for i in range(len(features)):
                    col = "coeff_" + str(i)
                    # dataframe[col] = 0.0
                    col_names.append(col)
                num_coeffs = len(features)
            coeff_array.append(features)
            start = start + 1

        # add empty data to account for the startup window
        zeros = []
        for i in range(self.dwt_window - 1):
            zeros.append(np.zeros(num_coeffs, dtype=float))
        coeff_array = zeros + coeff_array

        # Create a DataFrame with the column names
        df = pd.DataFrame(columns=col_names)

        # Merge the data into the DataFrame
        for array in coeff_array:
            df = df.append(array.reshape(1, -1), ignore_index=True)

        # copy the coefficients into the dataframe
        dataframe = pd.concat([dataframe, self.df_coeffs], axis=1, ignore_index=False)

        return dataframe
    '''

    def merge_data(self, dataframe: DataFrame) -> DataFrame:

        # merge df_coeffs into the main dataframe

        l1 = dataframe.shape[0]
        l2 = self.df_coeffs.shape[0]

        if l1 != l2:
            print(f"    **** size mismatch. len(dataframe)={l1} len(self.df_coeffs)={l2}")
        dataframe = pd.concat([dataframe, self.df_coeffs], axis=1, ignore_index=False)

        return dataframe

    def convert_dataframe(self, dataframe: DataFrame) -> DataFrame:
        df = dataframe.copy()
        # convert date column so that it can be scaled.
        if 'date' in df.columns:
            dates = pd.to_datetime(df['date'], utc=True)
            df['date'] = dates.astype('int64')

        df.fillna(0.0, inplace=True)

        df.set_index('date')
        df.reindex()

        # scale the dataframe
        self.scaler.fit(df)
        df = pd.DataFrame(self.scaler.transform(df), columns=df.columns)

        return df

    def train_model(self, dataframe: DataFrame):

        df = self.convert_dataframe(dataframe)

        # need to exclude the startup period at the front, and the lookahead period at the end

        df = df.iloc[self.startup_candle_count:-self.lookahead]
        y = dataframe['close'].iloc[self.startup_candle_count+self.lookahead:].to_numpy()

        # print(f"df: {df.shape} y:{y.shape}")

        # self.coeff_model = SVR(kernel='rbf', C=1.0, epsilon=0.1)
        # params = {'n_estimators': 100, 'max_depth': 4, 'min_samples_split': 2,
        #           'learning_rate': 0.1, 'loss': 'squared_error'}
        # self.coeff_model = GradientBoostingRegressor(**params)
        params = {'n_estimators': 100, 'max_depth': 4, 'learning_rate': 0.1}

        self.coeff_model = XGBRegressor(**params)
        self.coeff_model.fit(df, y)


    def predict(self, a: np.ndarray) -> np.float:

        y_pred = self.coeff_model.predict(a)

        return y_pred

    def add_predictions(self, dataframe: DataFrame) -> DataFrame:

        df = self.convert_dataframe(dataframe)

        dataframe['dwt_predict'] = self.coeff_model.predict(df)
        return dataframe

    def add_rolling_predictions(self, dataframe: DataFrame) -> DataFrame:
        df = self.convert_dataframe(dataframe)

        nrows = df.shape[0]
        start = 0

        dataframe['dwt_predict'] = dataframe['close']
        for i in range(nrows):
            end = start + self.dwt_window - 1
            slice = df.iloc[start:end]
            dataframe['dwt_predict'][start:end] = self.coeff_model.predict(slice)
            start = start + 1
        return dataframe

    ###################################

    """
    entry Signal
    """

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []
        dataframe.loc[:, 'enter_tag'] = ''


        # some trading volume
        conditions.append(dataframe['volume'] > 0)

        # Fisher/Williams in buy region
        conditions.append(dataframe['fisher_wr'] <= -0.5)

        # DWT triggers
        dwt_cond = (
            qtpylib.crossed_above(dataframe['model_diff'], self.entry_dwt_diff.value)
        )

        conditions.append(dwt_cond)

        # DWTs will spike on big gains, so try to constrain
        spike_cond = (
                dataframe['model_diff'] < 2.0 * self.entry_dwt_diff.value
        )
        conditions.append(spike_cond)

        # set entry tags
        dataframe.loc[dwt_cond, 'enter_tag'] += 'dwt_entry '

        if conditions:
            dataframe.loc[reduce(lambda x, y: x & y, conditions), 'enter_long'] = 1

        return dataframe

    ###################################

    """
    exit Signal
    """

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []
        dataframe.loc[:, 'exit_tag'] = ''

        if not self.enable_exit_signal.value:
            dataframe['exit_long'] = 0
            return dataframe

        # some volume
        conditions.append(dataframe['volume'] > 0)

        # Fisher/Williams in sell region
        conditions.append(dataframe['fisher_wr'] >= 0.5)

        # DWT triggers
        dwt_cond = (
            qtpylib.crossed_below(dataframe['model_diff'], self.exit_dwt_diff.value)
        )

        conditions.append(dwt_cond)

        # DWTs will spike on big gains, so try to constrain
        spike_cond = (
                dataframe['model_diff'] > 2.0 * self.exit_dwt_diff.value
        )
        conditions.append(spike_cond)

        # set exit tags
        dataframe.loc[dwt_cond, 'exit_tag'] += 'dwt_exit '

        if conditions:
            dataframe.loc[reduce(lambda x, y: x & y, conditions), 'exit_long'] = 1

        return dataframe

    ###################################

    """
    Custom Stoploss
    """

    # simplified version of custom trailing stoploss
    def custom_stoploss(self, pair: str, trade: 'Trade', current_time: datetime, current_rate: float,
                        current_profit: float, **kwargs) -> float:

        if current_profit > self.tstop_start.value:
            return current_profit * self.tstop_ratio.value

        # return min(-0.001, max(stoploss_from_open(0.05, current_profit), -0.99))
        return -0.99


    ###################################

    """
    Custom Exit
    (Note that this runs even if use_custom_stoploss is False)
    """

    # simplified version of custom exit

    def custom_exit(self, pair: str, trade: 'Trade', current_time: 'datetime', current_rate: float,
                    current_profit: float, **kwargs):

        dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
        last_candle = dataframe.iloc[-1].squeeze()

        # trade_dur = int((current_time.timestamp() - trade.open_date_utc.timestamp()) // 60)

        # check profit against ROI target. This sort of emulates the freqtrade roi approach, but is much simpler
        if self.use_profit_threshold.value:
            if (current_profit >= self.profit_threshold.value):
                return 'profit_threshold'


        # check loss against threshold. This sort of emulates the freqtrade stoploss approach, but is much simpler
        if self.use_loss_threshold.value:
            if (current_profit <= self.loss_threshold.value):
                return 'loss_threshold'

        # Mod: strong sell signal, in profit
        if (current_profit > 0) and (last_candle['fisher_wr'] > 0.98):
            return 'fwr_98'

        # Mod: Sell any positions at a loss if they are held for more than 'N' days.
        # if (current_profit < 0.0) and (current_time - trade.open_date_utc).days >= 7:
        if (current_time - trade.open_date_utc).days >= 7:
            return 'unclog'


        return None
