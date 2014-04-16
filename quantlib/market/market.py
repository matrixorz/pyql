from quantlib.termstructures.yields.api import (
    FixedRateBondHelper, DepositRateHelper, FuturesRateHelper, SwapRateHelper)

from quantlib.quotes import SimpleQuote

from quantlib.time.api import (Date, Period, Calendar, Years,
                               Days, JointCalendar, UnitedStates,
                               UnitedKingdom)
from quantlib.time.date import (code_to_frequency, pydate_from_qldate,
                                qldate_from_pydate)
from quantlib.time.daycounter import DayCounter

from quantlib.settings import Settings
from quantlib.indexes.api import IborIndex

from quantlib.util.converter import pydate_to_qldate

from quantlib.termstructures.yields.piecewise_yield_curve import \
    term_structure_factory

from quantlib.market.conventions.swap import SwapData
from quantlib.time.businessdayconvention import BusinessDayConvention

from quantlib.instruments.swap import VanillaSwap, PAYER
from quantlib.pricingengines.swap import DiscountingSwapEngine
from quantlib.time.schedule import Schedule, Forward
from quantlib.termstructures.yields.api import (
    YieldTermStructure)
import quantlib.time.imm as imm

from quantlib.time.api import Backward, Following


def libor_market(market='USD(NY)', **kwargs):
    m = IborMarket('USD Libor', market, **kwargs)
    return m


def next_imm_date(reference_date, tenor):
    """
    Third Wednesday of contract month
    """
    dt = qldate_from_pydate(reference_date)
    for k in range(tenor):
        tmp = imm.next_date(dt)
        dt = pydate_to_qldate(tmp)
    return pydate_from_qldate(dt)


def make_rate_helper(market, quote, reference_date=None):
    """
    Wrapper for deposit and swaps rate helpers makers
    TODO: class method of RateHelper?
    """

    rate_type, tenor, quote_value = quote

    if(rate_type == 'SWAP'):
        libor_index = market._floating_rate_index
        spread = SimpleQuote(0)
        fwdStart = Period(0, Days)
        helper = SwapRateHelper.from_tenor(
            quote_value,
            Period(tenor),
            market._floating_rate_index.fixing_calendar,
            code_to_frequency(market._params.fixed_leg_period),
            BusinessDayConvention.from_name(
                market._params.fixed_leg_convention),
            DayCounter.from_name(market._params.fixed_leg_daycount),
            libor_index, spread, fwdStart)
    elif(rate_type == 'DEP'):
        end_of_month = True
        helper = DepositRateHelper(
            quote_value,
            Period(tenor),
            market._params.settlement_days,
            market._floating_rate_index.fixing_calendar,
            market._floating_rate_index.business_day_convention,
            end_of_month,
            DayCounter.from_name(market._deposit_daycount))
    elif(rate_type == 'ED'):
        if reference_date is None:
            raise Exception("Reference date needed with ED Futures data")

        forward_date = next_imm_date(reference_date, tenor)

        helper = FuturesRateHelper(
            rate =SimpleQuote(quote_value),
            imm_date = qldate_from_pydate(forward_date),
            length_in_months = 3,
            calendar = market._floating_rate_index.fixing_calendar,
            convention = market._floating_rate_index.business_day_convention,
            end_of_month = True,
            day_counter = DayCounter.from_name(
                market._params.floating_leg_daycount))

    elif rate_type.startswith('ER'):
        # TODO For Euribor futures, we found it useful to supply the `imm_date`
        # parameter directly, instead of as a number of periods from the
        # evaluation date, as for ED futures. To achieve this, we pass the
        # `imm_date` in the `tenor` field of the quote.
        helper = FuturesRateHelper(
            rate=SimpleQuote(quote_value),
            imm_date=tenor,
            length_in_months=3,
            calendar=market._floating_rate_index.fixing_calendar,
            convention=market._floating_rate_index.business_day_convention,
            end_of_month=True,
            day_counter=DayCounter.from_name(
                market._params.floating_leg_daycount))
    else:
        raise Exception("Rate type %s not supported" % rate_type)

    return helper


def make_eurobond_helper(
        market, clean_price, coupons, tenor, issue_date, maturity):

    """ Wrapper for bond helpers.

    FIXME: This convenience method has some conventions specifically
    hardcoded for Eurobonds. These should be moved to the market.

    """

    # Create schedule based on market and bond parameters.
    index = market._floating_rate_index
    schedule = Schedule(
        issue_date,
        maturity,
        Period(tenor),
        index.fixing_calendar,
        index.business_day_convention,
        index.business_day_convention,
        Backward,  # Date generation rule
        index.end_of_month,
        )

    daycounter = DayCounter.from_name("Actual/Actual (Bond)")
    helper = FixedRateBondHelper(
        SimpleQuote(clean_price),
        market._params.settlement_days,
        100.0,
        schedule,
        coupons,
        daycounter,
        Following,  # Payment convention
        100.0,
        issue_date)

    return helper


class Market:
    """
    Abstract Market class.
    A Market is a virtual environment where financial assets are traded.
    It defines the conventions for quoting prices and yield,
    for measuring time, etc.
    """

    def __init__(self, name):
        self._name = name

    @property
    def name(self):
        return self._name


class FixedIncomeMarket(Market):
    """
    A Fixed Income Market, defined by:
    - a list of benchmarks instruments (deposits, FRA, swaps,
      EuroDollar futures, bonds)
    - a set of market conventions, needed to interpreted the quoted
      prices of benchmark instruments, and for computing
      derived quantities (yield curves)

    This class models an homogeneous market: It is assumed that the
    market conventions for all fixed rate instruments, including swaps,
    are all consistent. The conventions may vary between the fixed rate
    instruments and the deposit instruments.
    """

    pass


class IborMarket(FixedIncomeMarket):

    def __init__(self, name, market, **kwargs):

        params = SwapData.params(market)
        params = params._replace(**kwargs)
        self._params = params
        self._name = name
        self._market = market

        # floating rate index
        index = IborIndex.from_name(market, **kwargs)
        self._floating_rate_index = index

        self._deposit_daycount = params.floating_leg_daycount
        self._termstructure_daycount = 'ACT/365'

        self._eval_date = None
        self._quotes = None
        self._termstructure = None

        self._discount_term_structure = None
        self._forecast_term_structure = None

        self._rate_helpers = []
        self._quotes = []

    def __str__(self):
        return 'Fixed Income Market: %s' % self._name

    def _set_evaluation_date(self, dt_obs):
        if(~isinstance(dt_obs, Date)):
            dt_obs = pydate_to_qldate(dt_obs)
        settings = Settings()
        calendar = JointCalendar(UnitedStates(), UnitedKingdom())
        # must be a business day
        eval_date = calendar.adjust(dt_obs)
        settings.evaluation_date = eval_date
        self._eval_date = eval_date
        return eval_date

    def set_quotes(self, dt_obs, quotes):

        self._quotes.extend(quotes)
        eval_date = self._set_evaluation_date(dt_obs)

        for quote in quotes:
            # construct rate helper
            helper = make_rate_helper(self, quote, eval_date)
            self._rate_helpers.append(helper)

    def set_bonds(self, dt_obs, quotes):
        """ Supply the market with a set of bond quotes.

        The `quotes` parameter must be a list of quotes of the form
        (clean_price, coupons, tenor, issue_date, maturity). For more
        information about the format of the individual fields, see
        the documentation for :meth:`add_bond_quote`.

        """

        self._quotes.extend(quotes)
        self._set_evaluation_date(dt_obs)

        for quote in quotes:
            self.add_bond_quote(*quote)

    def add_bond_quote(
            self, clean_price, coupons, tenor, issue_date, maturity):
        """
        Add a bond quote to the market.

        Parameters
        ----------
        clean_price : real
            Clean price of the bond.
        coupons : real or list(real)
            Interest rates paid by the bond.
        tenor : str
            Tenor of the bond.
        issue_date, maturity : Date instance
            Issue date and maturity of the bond.

        """

        if not isinstance(coupons, (list, tuple)):
            coupons = [coupons]

        helper = make_eurobond_helper(
            self, clean_price, coupons, tenor, issue_date, maturity)
        self._rate_helpers.append(helper)

    @property
    def calendar(self):
        return self._params.calendar

    @property
    def settlement_days(self):
        return self._params.settlement_days

    @property
    def fixed_rate_frequency(self):
        return self._params.fixed_rate_frequency

    @property
    def fixed_rate_convention(self):
        return self._params.fixed_instrument_convention

    @property
    def fixed_rate_daycounter(self):
        return self._params.fixed_rate_daycounter

    @property
    def termstructure_daycounter(self):
        return self._termstructure_daycounter

    @property
    def reference_date(self):
        return 0

    @property
    def max_date(self):
        return 0

    def to_str(self):
        str = \
            "Ibor Market %s\n" % self._name + \
            "Number of settlement days: %d\n" % self._params.settlement_days +\
            "Fixed rate frequency: %s\n" % self._params.fixed_rate_frequency +\
            "Fixed rate convention: %s\n" % self._params.fixed_instrument_convention +\
            "Fixed rate daycount: %s\n" % self._params.fixed_instrument_daycounter +\
            "Term structure daycount: %s\n" % self._termstructure_daycount + \
            "Floating rate index: %s\n" % self._floating_rate_index + \
            "Deposit daycount: %s\n" % self._deposit_daycount + \
            "Calendar: %s\n" % self._params.calendar

        return str

    def bootstrap_term_structure(self, interpolator='loglinear'):
        tolerance = 1.0e-15
        settings = Settings()
        calendar = JointCalendar(UnitedStates(), UnitedKingdom())
        # must be a business day
        eval_date = self._eval_date
        settings.evaluation_date = eval_date
        settlement_days = self._params.settlement_days
        settlement_date = calendar.advance(eval_date, settlement_days, Days)
        # must be a business day
        settlement_date = calendar.adjust(settlement_date)
        ts = term_structure_factory(
            'discount', interpolator,
            settlement_date, self._rate_helpers,
            DayCounter.from_name(self._termstructure_daycount),
            tolerance)
        self._term_structure = ts
        self._discount_term_structure = YieldTermStructure(relinkable=True)
        self._discount_term_structure.link_to(ts)

        self._forecast_term_structure = YieldTermStructure(relinkable=True)
        self._forecast_term_structure.link_to(ts)

        return ts

    def discount(self, date_maturity, extrapolate=True):
        return self._discount_term_structure.discount(date_maturity)

    def create_fixed_float_swap(self, settlement_date, length, fixed_rate,
                                floating_spread, **kwargs):
        """
        Create a fixed-for-float swap given:
        - settlement date
        - length in years
        - additional arguments to modify market default parameters
        """

        _params = self._params._replace(**kwargs)

        index = IborIndex.from_name(self._market,
                                    self._forecast_term_structure,
                                    **kwargs)

        swap_type = PAYER
        nominal = 100.0
        fixed_convention = \
            BusinessDayConvention.from_name(_params.fixed_leg_convention)
        floating_convention = \
            BusinessDayConvention.from_name(_params.floating_leg_convention)
        fixed_frequency = \
            code_to_frequency(_params.fixed_leg_period)
        floating_frequency = code_to_frequency(_params.floating_leg_period)
        fixed_daycount = DayCounter.from_name(_params.fixed_leg_daycount)
        float_daycount = DayCounter.from_name(_params.floating_leg_daycount)
        calendar = Calendar.from_name(_params.calendar)

        maturity = calendar.advance(settlement_date, length, Years,
                                    convention=floating_convention)

        fixed_schedule = Schedule(settlement_date, maturity,
                                  Period(fixed_frequency), calendar,
                                  fixed_convention, fixed_convention,
                                  Forward, False)

        float_schedule = Schedule(settlement_date, maturity,
                                  Period(floating_frequency),
                                  calendar, floating_convention,
                                  floating_convention,
                                  Forward, False)

        swap = VanillaSwap(swap_type, nominal, fixed_schedule, fixed_rate,
                           fixed_daycount, float_schedule, index,
                           floating_spread, float_daycount, fixed_convention)

        engine = DiscountingSwapEngine(self._discount_term_structure,
                                       False,
                                       settlementDate=settlement_date,
                                       npvDate=settlement_date)

        swap.set_pricing_engine(engine)

        return swap
