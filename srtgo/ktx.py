"""
korail2.korail2
~~~~~~~~~~~~~~~

:copyright: (c) 2014 by Taehoon Kim.
:license: BSD, see LICENSE for more details.
"""

import base64
try:
    import curl_cffi
    HAS_CURL_CFFI = True
except ImportError:
    import requests
    HAS_CURL_CFFI = False
import itertools
import json
import os
import re
import time
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from datetime import datetime, timedelta
from functools import reduce

from . import korail_version
from .dynapath import (
    DYNAPATH_HEADER_NAME,
    DynapathTokenSettings,
    generate_dynapath_device_id,
    generate_dynapath_token,
)


# Constants
EMAIL_REGEX = re.compile(r"[^@]+@[^@]+\.[^@]+")
PHONE_NUMBER_REGEX = re.compile(r"(\d{3})-(\d{3,4})-(\d{4})")

USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 15; Android)"

# 응답 없이 매달려 있지 않도록: 넘기면 실패시키고 다음 시도에서 재요청한다.
REQUEST_TIMEOUT = 30

DEFAULT_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "User-Agent": USER_AGENT,
    "Host": "smart.letskorail.com",
    "Connection": "Keep-Alive",
    "Accept-Encoding": "gzip",
}

# 코레일 앱 버전 기본값. ~/.srtgo/korail.json 의 {"app_version", "version"} 이 있으면 그걸 쓴다.
# 서버가 옛 버전을 막으면(-8204) korail_version 이 맞는 값을 찾아 그 파일에 저장한다.
KORAIL_APP_VERSION = "7.0.10"  # 2026-10-21 코레일+ 앱 기준
KORAIL_API_VERSION = "250601003"


def korail_versions() -> tuple[str, str]:
    conf = korail_version.load_config()
    app = str(conf.get("app_version") or KORAIL_APP_VERSION).strip()
    api = str(conf.get("version") or KORAIL_API_VERSION).strip()
    return app, api


KORAIL_ROOT = "https://smart.letskorail.com:443"
KORAIL_MOBILE = f"{KORAIL_ROOT}/classes/com.korail.mobile"
API_ENDPOINTS = {
    "login": f"{KORAIL_MOBILE}.login.Login",
    "logout": f"{KORAIL_MOBILE}.common.logout",
    "search_schedule": f"{KORAIL_MOBILE}.seatMovie.ScheduleView",
    "reserve": f"{KORAIL_MOBILE}.certification.TicketReservation",
    "cancel": f"{KORAIL_MOBILE}.reservationCancel.ReservationCancelChk",
    "myticketseat": f"{KORAIL_MOBILE}.refunds.SelTicketInfo",
    "myticketlist": f"{KORAIL_MOBILE}.myTicket.MyTicketList",
    "myreservationview": f"{KORAIL_MOBILE}.reservation.ReservationView",
    "myreservationlist": f"{KORAIL_MOBILE}.certification.ReservationList",
    "pay": f"{KORAIL_MOBILE}.payment.ReservationPayment",
    "refund": f"{KORAIL_MOBILE}.refunds.RefundsRequest",
    "code": f"{KORAIL_MOBILE}.common.code.do",
}


# Schedule classes
class Schedule:
    """Base class for train schedules"""

    def __init__(self, data):
        self.train_type = data.get("h_trn_clsf_cd")
        self.train_type_name = data.get("h_trn_clsf_nm")
        self.train_group = data.get("h_trn_gp_cd")
        self.train_no = data.get("h_trn_no")
        self.delay_time = data.get("h_expct_dlay_hr")

        self.dep_name = data.get("h_dpt_rs_stn_nm")
        self.dep_code = data.get("h_dpt_rs_stn_cd")
        self.dep_date = data.get("h_dpt_dt")
        self.dep_time = data.get("h_dpt_tm")

        self.arr_name = data.get("h_arv_rs_stn_nm")
        self.arr_code = data.get("h_arv_rs_stn_cd")
        self.arr_date = data.get("h_arv_dt")
        self.arr_time = data.get("h_arv_tm")

        self.run_date = data.get("h_run_dt")

        # 코레일+ 7.0.8부터 예약 요청에 필수: 출발·도착역의 편성/운행 순서.
        self.dep_stn_cons_ordr = data.get("h_dpt_stn_cons_ordr", "000000")
        self.dep_stn_run_ordr = data.get("h_dpt_stn_run_ordr", "000000")
        self.arr_stn_cons_ordr = data.get("h_arv_stn_cons_ordr", "000000")
        self.arr_stn_run_ordr = data.get("h_arv_stn_run_ordr", "000000")

    def __repr__(self):
        dep_time = f"{self.dep_time[:2]}:{self.dep_time[2:4]}"
        arr_time = f"{self.arr_time[:2]}:{self.arr_time[2:4]}"

        dep_date = f"{int(self.dep_date[4:6]):02d}/{int(self.dep_date[6:]):02d}"

        train_line = f"[{self.train_type_name[:3]} {self.train_no}]"

        return (
            f"{train_line:<11s}"
            f"{dep_date} {dep_time}~{arr_time}  "
            f"{self.dep_name}~{self.arr_name}"
        )


class Train(Schedule):
    """Train schedule with seat availability"""

    def __init__(self, data):
        super().__init__(data)
        self.reserve_possible = data.get("h_rsv_psb_flg")
        self.reserve_possible_name = data.get("h_rsv_psb_nm")
        self.special_seat = data.get("h_spe_rsv_cd")
        self.general_seat = data.get("h_gen_rsv_cd")
        self.wait_reserve_flag = data.get("h_wait_rsv_flg")
        if self.wait_reserve_flag:
            self.wait_reserve_flag = int(self.wait_reserve_flag)

    def __repr__(self):
        repr_str = super().__repr__()

        dep_time = f"{self.dep_time[:2]}:{self.dep_time[2:4]}"
        arr_time = f"{self.arr_time[:2]}:{self.arr_time[2:4]}"

        duration = (int(self.arr_time[:2]) * 60 + int(self.arr_time[2:4])) - (
            int(self.dep_time[:2]) * 60 + int(self.dep_time[2:4])
        )

        if duration < 0:
            duration += 24 * 60

        if self.reserve_possible_name:
            repr_str += f"  특실 {'가능' if self.has_special_seat() else '매진'}"
            repr_str += f", 일반실 {'가능' if self.has_general_seat() else '매진'}"
            if self.wait_reserve_flag >= 0:
                repr_str += f", 예약대기 {'가능' if self.has_general_waiting_list() else '매진'}"
        repr_str += f" ({duration:>3d}분)"
        return repr_str

    def has_special_seat(self):
        return self.special_seat == "11"

    def has_general_seat(self):
        return self.general_seat == "11"

    def has_seat(self):
        return self.has_general_seat() or self.has_special_seat()

    def has_waiting_list(self):
        return self.has_general_waiting_list()

    def has_general_waiting_list(self):
        return self.wait_reserve_flag == 9


class Ticket(Train):
    """Train ticket information"""

    def __init__(self, data):
        raw_data = data["ticket_list"][0]["train_info"][0]
        super().__init__(raw_data)
        self.seat_no_end = raw_data.get("h_seat_no_end")
        self.seat_no_count = int(raw_data.get("h_seat_cnt"))
        self.buyer_name = raw_data.get("h_buy_ps_nm")
        self.sale_date = raw_data.get("h_orgtk_sale_dt")
        self.pnr_no = raw_data.get("h_pnr_no")
        self.sale_info1 = raw_data.get("h_orgtk_wct_no")
        self.sale_info2 = raw_data.get("h_orgtk_ret_sale_dt")
        self.sale_info3 = raw_data.get("h_orgtk_sale_sqno")
        self.sale_info4 = raw_data.get("h_orgtk_ret_pwd")
        self.price = int(raw_data.get("h_rcvd_amt"))
        self.car_no = raw_data.get("h_srcar_no")
        self.seat_no = raw_data.get("h_seat_no")

    def __repr__(self):
        repr_str = super(Train, self).__repr__()
        repr_str += f" => {self.car_no}호"
        if int(self.seat_no_count) != 1:
            repr_str += f" {self.seat_no}~{self.seat_no_end}"
        else:
            repr_str += f" {self.seat_no}"
        repr_str += f", {self.price}원"
        return repr_str

    def get_ticket_no(self):
        return "-".join(
            map(
                str,
                (self.sale_info1, self.sale_info2, self.sale_info3, self.sale_info4),
            )
        )


class Reservation(Train):
    """Train reservation information"""

    def __init__(self, data):
        super().__init__(data)
        self.dep_date = data.get("h_run_dt")
        self.arr_date = data.get("h_run_dt")
        self.rsv_id = data.get("h_pnr_no")
        self.seat_no_count = int(data.get("h_tot_seat_cnt"))
        self.buy_limit_date = data.get("h_ntisu_lmt_dt")
        self.buy_limit_time = data.get("h_ntisu_lmt_tm")
        self.price = int(data.get("h_rsv_amt"))
        self.journey_no = data.get("txtJrnySqno", "001")
        self.journey_cnt = data.get("txtJrnyCnt", "01")
        self.rsv_chg_no = data.get("hidRsvChgNo", "00000")
        self.is_waiting = (
            self.buy_limit_date == "00000000" or self.buy_limit_time == "235959"
        )

    def __repr__(self):
        repr_str = super().__repr__()
        repr_str += f", {self.price}원({self.seat_no_count}석)"
        if self.is_waiting:
            repr_str += ", 예약대기"
        else:
            buy_limit_time = f"{self.buy_limit_time[:2]}:{self.buy_limit_time[2:4]}"
            buy_limit_date = (
                f"{int(self.buy_limit_date[4:6])}월 {int(self.buy_limit_date[6:])}일"
            )
            repr_str += f", 구입기한 {buy_limit_date} {buy_limit_time}"
        return repr_str


class Seat:
    """Train seat information"""

    def __init__(self, data: dict):
        self.car = data.get("h_srcar_no")
        self.seat = data.get("h_seat_no")
        self.seat_type = data.get("h_psrm_cl_nm")
        self.passenger_type = data.get("h_psg_tp_dv_nm")
        self.price = int(data.get("h_rcvd_amt", 0))
        self.original_price = int(data.get("h_seat_prc", 0))
        self.discount = int(data.get("h_dcnt_amt", 0))
        self.is_waiting = self.seat == ""

    def __repr__(self):
        if self.is_waiting:
            return (
                f"예약대기 ({self.seat_type}) {self.passenger_type}"
                f"[{self.price}원({self.discount}원 할인)]"
            )
        else:
            return (
                f"{self.car}호차 {self.seat} ({self.seat_type}) {self.passenger_type} "
                f"[{self.price}원({self.discount}원 할인)]"
            )


# Passenger classes
class Passenger:
    """Base class for passengers"""

    def __init_internal__(
        self, typecode, count=1, discount_type="000", card="", card_no="", card_pw=""
    ):
        self.typecode = typecode
        self.count = count
        self.discount_type = discount_type
        self.card = card
        self.card_no = card_no
        self.card_pw = card_pw

    @staticmethod
    def reduce(passenger_list):
        if not all(isinstance(x, Passenger) for x in passenger_list):
            raise TypeError("Passengers must be based on Passenger")
        groups = itertools.groupby(passenger_list, lambda x: x.group_key())
        return list(
            filter(
                lambda x: x.count > 0,
                [reduce(lambda a, b: a + b, g) for k, g in groups],
            )
        )

    def __add__(self, other):
        if not isinstance(other, self.__class__):
            raise TypeError("Cannot add different passenger types")
        if self.group_key() != other.group_key():
            raise TypeError(
                f"Cannot add passengers with different group keys: {self.group_key()} vs {other.group_key()}"
            )
        return self.__class__(
            count=self.count + other.count,
            discount_type=self.discount_type,
            card=self.card,
            card_no=self.card_no,
            card_pw=self.card_pw,
        )

    def group_key(self):
        return f"{self.typecode}_{self.discount_type}_{self.card}_{self.card_no}_{self.card_pw}"

    def get_dict(self, index):
        index = str(index)
        return {
            f"txtPsgTpCd{index}": self.typecode,
            f"txtDiscKndCd{index}": self.discount_type,
            f"txtCompaCnt{index}": self.count,
            f"txtCardCode_{index}": self.card,
            f"txtCardNo_{index}": self.card_no,
            f"txtCardPw_{index}": self.card_pw,
        }


class AdultPassenger(Passenger):
    def __init__(self, count=1, discount_type="000", card="", card_no="", card_pw=""):
        Passenger.__init_internal__(
            self, "1", count, discount_type, card, card_no, card_pw
        )


class ChildPassenger(Passenger):
    def __init__(self, count=1, discount_type="000", card="", card_no="", card_pw=""):
        Passenger.__init_internal__(
            self, "3", count, discount_type, card, card_no, card_pw
        )


class ToddlerPassenger(Passenger):
    def __init__(self, count=1, discount_type="321", card="", card_no="", card_pw=""):
        Passenger.__init_internal__(
            self, "3", count, discount_type, card, card_no, card_pw
        )


class SeniorPassenger(Passenger):
    def __init__(self, count=1, discount_type="131", card="", card_no="", card_pw=""):
        Passenger.__init_internal__(
            self, "1", count, discount_type, card, card_no, card_pw
        )


class Disability1To3Passenger(Passenger):
    def __init__(self, count=1, discount_type="111", card="", card_no="", card_pw=""):
        Passenger.__init_internal__(
            self, "1", count, discount_type, card, card_no, card_pw
        )


class Disability4To6Passenger(Passenger):
    def __init__(self, count=1, discount_type="112", card="", card_no="", card_pw=""):
        Passenger.__init_internal__(
            self, "1", count, discount_type, card, card_no, card_pw
        )


# Options
class TrainType:
    KTX = "100"
    SAEMAEUL = "101"
    MUGUNGHWA = "102"
    TONGGUEN = "103"
    NURIRO = "102"
    ALL = "109"
    AIRPORT = "105"
    KTX_SANCHEON = "100"
    ITX_SAEMAEUL = "101"
    ITX_CHEONGCHUN = "104"


class ReserveOption:
    GENERAL_FIRST = "GENERAL_FIRST"
    GENERAL_ONLY = "GENERAL_ONLY"
    SPECIAL_FIRST = "SPECIAL_FIRST"
    SPECIAL_ONLY = "SPECIAL_ONLY"


# Korail errors
class KorailError(Exception):
    """Base class for Korail errors"""

    def __init__(self, msg, code=None):
        self.msg = msg
        self.code = code

    def __str__(self):
        return f"{self.msg} ({self.code})"


class NeedToLoginError(KorailError):
    codes = {"P058"}

    def __init__(self, code=None):
        super().__init__("Need to Login", code)


class NoResultsError(KorailError):
    codes = {"P100", "WRG000000", "WRD000061", "WRT300005"}

    def __init__(self, code=None):
        super().__init__("No Results", code)


class SoldOutError(KorailError):
    codes = {"IRT010110", "ERR211161"}

    def __init__(self, code=None):
        super().__init__("Sold out", code)


class NetFunnelError(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return self.msg


# NetFunnel
class NetFunnelHelper:
    NETFUNNEL_URL = "http://nf.letskorail.com/ts.wseq"

    WAIT_STATUS_PASS = "200"
    WAIT_STATUS_FAIL = "201"
    ALREADY_COMPLETED = "502"

    OP_CODE = {
        "getTidchkEnter": "5101",
        "chkEnter": "5002",
        "setComplete": "5004",
    }

    DEFAULT_HEADERS = {
        "Host": "nf.letskorail.com",
        "Connection": "Keep-Alive",
        "User-Agent": "Apache-HttpClient/UNAVAILABLE (java 1.4)",
    }

    def __init__(self):
        if HAS_CURL_CFFI:
            self._session = curl_cffi.Session(impersonate="chrome131_android", timeout=REQUEST_TIMEOUT)
        else:
            self._session = requests.session()
        self._session.headers.update(self.DEFAULT_HEADERS)
        self._cached_key = None
        self._last_fetch_time = 0
        self._cache_ttl = 50  # 50 seconds

    def run(self):
        current_time = time.time()
        if self._is_cache_valid(current_time):
            return self._cached_key

        try:
            status, self._cached_key, nwait = self._start()
            self._last_fetch_time = current_time

            while status == self.WAIT_STATUS_FAIL:
                print(f"\r현재 {nwait}명 대기중...", end="", flush=True)
                time.sleep(1)
                status, self._cached_key, nwait = self._check()

            # Try completing once
            status, _, _ = self._complete()
            if status == self.WAIT_STATUS_PASS or status == self.ALREADY_COMPLETED:
                return self._cached_key

            self.clear()
            raise NetFunnelError("Failed to complete NetFunnel")

        except Exception as ex:
            self.clear()
            raise NetFunnelError(str(ex))

    def clear(self):
        self._cached_key = None
        self._last_fetch_time = 0

    def _start(self):
        return self._make_request("getTidchkEnter")

    def _check(self):
        return self._make_request("chkEnter")

    def _complete(self):
        return self._make_request("setComplete")

    def _make_request(self, opcode: str):
        params = self._build_params(self.OP_CODE[opcode])
        response = self._parse(
            self._session.get(self.NETFUNNEL_URL, params=params).text
        )
        return response.get("status"), response.get("key"), response.get("nwait")

    def _build_params(self, opcode: str, key: str = None) -> dict:
        params = {"opcode": opcode}

        if opcode in (self.OP_CODE["getTidchkEnter"], self.OP_CODE["chkEnter"]):
            params.update({"sid": "service_1", "aid": "act_8"})
            if opcode == self.OP_CODE["chkEnter"]:
                params.update({"key": key or self._cached_key, "ttl": "1"})
        elif opcode == self.OP_CODE["setComplete"]:
            params["key"] = key or self._cached_key

        return params

    def _parse(self, response: str) -> dict:
        status, params_str = response.split(":", 1)
        if not params_str:
            raise NetFunnelError("Failed to parse NetFunnel response")

        params = dict(
            param.split("=", 1) for param in params_str.split("&") if "=" in param
        )
        params["status"] = status
        return params

    def _is_cache_valid(self, current_time: float) -> bool:
        return bool(
            self._cached_key
            and (current_time - self._last_fetch_time) < self._cache_ttl
        )


def _human_mode():
    """SRTGO_HUMAN_FETCH=0 이면 예전 앱 방식(로그인·조회·예약 모두)."""
    return os.environ.get("SRTGO_HUMAN_FETCH", "1") != "0"


class Korail:
    """Main Korail API interface"""

    def __init__(self, korail_id, korail_pw, auto_login=True, verbose=False, version_guard=True):
        if HAS_CURL_CFFI:
            self._session = curl_cffi.Session(impersonate="chrome131_android", timeout=REQUEST_TIMEOUT)
        else:
            self._session = requests.session()
        self._session.headers.update(DEFAULT_HEADERS)
        if version_guard:
            # -8204(옛 앱 버전)면 맞는 버전을 찾아 저장하고 다시 보낸다.
            self._session = korail_version.GuardedSession(self._session, self)
        self._device = "AD"
        # Version(API 계약)과 AppVersion(앱 버전)이 낮으면 서버가 -8204로 막는다.
        self._app_version, self._version = korail_versions()
        self._key = "korail1234567890"
        self._idx = None
        self._dynapath_device_id = generate_dynapath_device_id()
        self._dynapath_app_start_ts = str(int(time.time() * 1000))
        self.korail_id = korail_id
        self.korail_pw = korail_pw
        self.verbose = verbose
        self.logined = False
        # 로그인 실패 사유(코레일 응답의 h_msg_txt). login()은 실패해도 예외를 던지지
        # 않으므로, 호출하는 쪽이 logined 와 함께 이걸 보고 실패를 알아야 한다.
        self.login_error = None
        self.membership_number = None
        self.name = None
        self.email = None
        self.phone_number = None
        if auto_login:
            self.login(korail_id, korail_pw)

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[*] {msg}")

    def _dynapath_header(self) -> dict:
        settings = DynapathTokenSettings(
            device_id=self._dynapath_device_id,
            app_start_ts=self._dynapath_app_start_ts,
        )
        return {DYNAPATH_HEADER_NAME: generate_dynapath_token(settings)}

    def __enc_password(self, password):
        url = API_ENDPOINTS["code"]
        # 공통 파라미터와 화면 정보가 빠지면 세션에 맞지 않는 키가 와서 비밀번호가 틀린 것으로 나온다.
        data = {
            "Device": self._device,
            "Version": self._version,
            "AppVersion": self._app_version,
            "Key": self._key,
            "code": "app.login.cphd",
            "deviceWidth": "1440",
            "deviceHeight": "3120",
            "OSVersion": "37",
        }
        r = self._session.post(url, data=data)
        j = json.loads(r.text)

        if j["strResult"] == "SUCC" and j.get("app.login.cphd"):
            self._idx = j["app.login.cphd"]["idx"]
            key = j["app.login.cphd"]["key"]
            encrypt_key = key.encode("utf-8")
            iv = key[:16].encode("utf-8")
            cipher = AES.new(encrypt_key, AES.MODE_CBC, iv)
            padded_data = pad(password.encode("utf-8"), AES.block_size)
            # 앱과 같은 이중 Base64: 안쪽은 표준(줄바꿈 없음), 바깥은 URL-safe에 76자 줄바꿈.
            inner = base64.b64encode(cipher.encrypt(padded_data)).decode("ascii")
            outer = base64.urlsafe_b64encode(inner.encode("ascii")).decode("ascii")
            return "\n".join(outer[i : i + 76] for i in range(0, len(outer), 76)) + "\n"
        return False

    def _check_service(self):
        """로그인 전 서비스 상태 확인. 앱과 같은 순서로 세션 쿠키를 받아 둔다."""
        try:
            self._session.post(
                f"{KORAIL_ROOT}/file/CACHE/MobileService.cache",
                data={"timeStamp": str(int(time.time() * 1000))},
            )
        except Exception as e:
            self._log(f"check_service failed (ignored): {e}")

    def login(self, korail_id=None, korail_pw=None):
        if korail_id:
            self.korail_id = korail_id
        if korail_pw:
            self.korail_pw = korail_pw

        if _human_mode():
            # 앱 로그인은 -8204 로 막힌다. 코레일 웹 로그인 화면에 사람처럼 입력한다(이미 로그인돼 있으면 입력하지 않는다).
            from . import human_client

            ok, why = human_client.login_with(self.korail_id, self.korail_pw)
            self.logined = bool(ok)
            self.login_error = None if ok else why
            if ok:
                self.name = self.name or ""
            return bool(ok)

        self._check_service()

        txt_input_flg = (
            "5"
            if EMAIL_REGEX.match(self.korail_id)
            else "4"
            if PHONE_NUMBER_REGEX.match(self.korail_id)
            else "2"
        )

        data = {
            "Device": self._device,
            "Version": self._version,
            "AppVersion": self._app_version,
            "Key": self._key,
            "txtInputFlg": txt_input_flg,
            "txtMemberNo": self.korail_id,
            "txtPwd": self.__enc_password(self.korail_pw),
            "checkValidPw": "Y",
            "idx": self._idx,
        }

        r = self._session.post(
            API_ENDPOINTS["login"], data=data, headers=self._dynapath_header()
        )
        self._log(r.text)
        j = json.loads(r.text)

        if j["strResult"] == "SUCC" and j.get("strMbCrdNo"):
            # self._key = j['Key']
            self.membership_number = j["strMbCrdNo"]
            # 이름·메일·전화는 표시용이라 빠져 있어도 로그인은 성공으로 본다.
            self.name = j.get("strCustNm") or ""
            self.email = j.get("strEmailAdr") or ""
            self.phone_number = j.get("strCpNo") or ""
            print(
                f"로그인 성공: {self.name} (멤버십번호: {self.membership_number}, 전화번호: {self.phone_number})"
            )
            self.logined = True
            self.login_error = None
            return True
        self.logined = False
        self.login_error = j.get("h_msg_txt") or "코레일 로그인 실패 (사유 미상)"
        return False

    def logout(self):
        r = self._session.get(API_ENDPOINTS["logout"])
        self._log(r.text)
        self.logined = False

    def _result_check(self, j):
        if j.get("strResult") == "FAIL":
            h_msg_cd = j.get("h_msg_cd")
            h_msg_txt = j.get("h_msg_txt")
            for error in (NoResultsError, NeedToLoginError, SoldOutError):
                if h_msg_cd in error.codes:
                    raise error(h_msg_cd)
            raise KorailError(h_msg_txt, h_msg_cd)
        return True

    def search_train(
        self,
        dep,
        arr,
        date=None,
        time=None,
        train_type=TrainType.ALL,
        passengers=None,
        include_no_seats=False,
        include_waiting_list=False,
    ):
        # 앱 주소가 -8204 로 막혀 있어, 열차 조회는 코레일 웹을 사람처럼 입력하는 통로로 보낸다.
        # SRTGO_HUMAN_FETCH=0 이면 예전 앱 방식으로 되돌린다. 예매·결제는 그대로 앱 방식이다.
        if os.environ.get("SRTGO_HUMAN_FETCH", "1") != "0":
            from . import human_client

            return human_client.search_train(
                dep, arr, date, time,
                train_type=train_type,
                include_no_seats=include_no_seats,
                include_waiting_list=include_waiting_list,
            )
        return self.search_train_app(
            dep, arr, date, time, train_type, passengers, include_no_seats, include_waiting_list
        )

    def search_train_app(
        self,
        dep,
        arr,
        date=None,
        time=None,
        train_type=TrainType.ALL,
        passengers=None,
        include_no_seats=False,
        include_waiting_list=False,
    ):
        """앱 방식 조회(옛 방식). 앱 버전 시험 같은 곳에서 직접 부른다."""
        kst_now = datetime.now() + timedelta(hours=9)
        date = date or kst_now.strftime("%Y%m%d")
        time = time or kst_now.strftime("%H%M%S")
        passengers = passengers or [AdultPassenger()]
        passengers = Passenger.reduce(passengers)

        counts = {
            "adult": sum(p.count for p in passengers if isinstance(p, AdultPassenger)),
            "child": sum(p.count for p in passengers if isinstance(p, ChildPassenger)),
            "toddler": sum(
                p.count for p in passengers if isinstance(p, ToddlerPassenger)
            ),
            "senior": sum(
                p.count for p in passengers if isinstance(p, SeniorPassenger)
            ),
            "disability1to3": sum(
                p.count for p in passengers if isinstance(p, Disability1To3Passenger)
            ),
            "disability4to6": sum(
                p.count for p in passengers if isinstance(p, Disability4To6Passenger)
            ),
        }

        data = {
            "Device": self._device,
            "Version": self._version,
            "AppVersion": self._app_version,
            "Key": self._key,
            "txtMenuId": "11",
            "radJobId": "1",
            "selGoTrain": train_type,
            "txtTrnGpCd": train_type,
            "txtGoStart": dep,
            "txtGoEnd": arr,
            "txtGoAbrdDt": date,
            "txtGoHour": time,
            "txtPsgFlg_1": counts["adult"],
            "txtPsgFlg_2": counts["child"] + counts["toddler"],
            "txtPsgFlg_3": counts["senior"],
            "txtPsgFlg_4": counts["disability1to3"],
            "txtPsgFlg_5": counts["disability4to6"],
            "txtSeatAttCd_2": "000",
            "txtSeatAttCd_3": "000",
            "txtSeatAttCd_4": "015",
            "ebizCrossCheck": "N",
            "srtCheckYn": "N",  # SRT 함께 보기
            "rtYn": "N",  # 왕복
            "adjStnScdlOfrFlg": "N",  # 인접역 보기
            "mbCrdNo": self.membership_number,
            "qryDvCd": "1",  # 조회 구분(직통). 7.0.8부터 필수
        }

        r = self._session.post(
            API_ENDPOINTS["search_schedule"], data=data, headers=self._dynapath_header()
        )
        self._log(r.text)
        j = json.loads(r.text)

        if self._result_check(j):
            trains = [
                Train(info) for info in j.get("trn_infos", {}).get("trn_info", [])
            ]
            filter_fns = [lambda x: x.has_seat()]

            if include_no_seats:
                filter_fns.append(lambda x: not x.has_seat())
            if include_waiting_list:
                filter_fns.append(lambda x: x.has_waiting_list())

            trains = [t for t in trains if any(f(t) for f in filter_fns)]

            if not trains:
                raise NoResultsError()

            return trains

    def reserve(self, train, passengers=None, option=ReserveOption.GENERAL_FIRST):
        reserving_seat = train.has_seat() or train.wait_reserve_flag < 0
        if reserving_seat:
            is_special_seat = {
                ReserveOption.GENERAL_ONLY: False,
                ReserveOption.SPECIAL_ONLY: True,
                ReserveOption.GENERAL_FIRST: not train.has_general_seat(),
                ReserveOption.SPECIAL_FIRST: train.has_special_seat(),
            }[option]
        else:
            is_special_seat = {
                ReserveOption.GENERAL_ONLY: False,
                ReserveOption.SPECIAL_ONLY: True,
                ReserveOption.GENERAL_FIRST: False,
                ReserveOption.SPECIAL_FIRST: True,
            }[option]

        passengers = passengers or [AdultPassenger()]
        passengers = Passenger.reduce(passengers)
        cnt = sum(p.count for p in passengers)

        # 앱 주소가 -8204 로 막혀 있어, 예약은 코레일 웹을 사람처럼 눌러 결제 전 단계까지만 한다(결제는 사람).
        if _human_mode():
            from . import human_client

            if not reserving_seat:
                raise human_client.HumanReserveError("웹 예약은 예약대기를 지원하지 않습니다", definite=True)
            if cnt != 1 or not isinstance(passengers[0], AdultPassenger):
                raise human_client.HumanReserveError("웹 예약은 어른 1명만 지원합니다", definite=True)
            return human_client.reserve(
                train, special=is_special_seat, creds=(self.korail_id, self.korail_pw)
            )

        data = {
            "Device": self._device,
            "Version": self._version,
            "AppVersion": self._app_version,
            "Key": self._key,
            "txtMenuId": "11",
            "txtJobId": "1101" if reserving_seat else "1102",
            "hidFreeFlg": "N",
            "txtStndFlg": "N",
            "txtTotPsgCnt": cnt,
            "txtSeatAttCd1": "000",
            "txtSeatAttCd2": "000",
            "txtSeatAttCd3": "000",
            "txtSeatAttCd4": "015",
            "txtSeatAttCd5": "000",
            "txtJrnyCnt": "1",
            "txtSrcarCnt": "0",
            # 7.0.8: 여정에 역 편성/운행 순서가 필수, 쓰지 않는 두 번째 여정 필드는 보내지 않는다.
            "txtJrnyTpCd1": "11",
            "txtJrnySqno1": "001",
            "txtTrnNo1": train.train_no,
            "txtTrnClsfCd1": train.train_type,
            "txtTrnGpCd1": train.train_group,
            "txtRunDt1": train.run_date,
            "txtDptDt1": train.dep_date,
            "txtDptTm1": train.dep_time,
            "txtDptRsStnCd1": train.dep_code,
            "txtDptStnConsOrdr1": train.dep_stn_cons_ordr,
            "txtDptStnRunOrdr1": train.dep_stn_run_ordr,
            "txtArvRsStnCd1": train.arr_code,
            "txtArvStnConsOrdr1": train.arr_stn_cons_ordr,
            "txtArvStnRunOrdr1": train.arr_stn_run_ordr,
            "txtChgFlg1": "N",
            "txtPsrmClCd1": "2" if is_special_seat else "1",
        }

        for i, psg in enumerate(passengers, 1):
            data.update(psg.get_dict(i))

        r = self._session.post(
            API_ENDPOINTS["reserve"], data=data, headers=self._dynapath_header()
        )
        self._log(r.text)
        j = json.loads(r.text)
        if self._result_check(j):
            rsv_id = j.get("h_pnr_no")
            reservation = self.reservations(rsv_id)
            return reservation
        else:
            raise SoldOutError()

    def tickets(self):
        data = {
            "Device": self._device,
            "Version": self._version,
            "Key": self._key,
            "txtDeviceId": "",
            "txtIndex": "1",
            "h_page_no": "1",
            "h_abrd_dt_from": "",
            "h_abrd_dt_to": "",
            "hiduserYn": "Y",
        }

        r = self._session.get(API_ENDPOINTS["myticketlist"], params=data)
        self._log(r.text)
        j = json.loads(r.text)
        try:
            if self._result_check(j):
                tickets = []
                for info in j.get("reservation_list", []):
                    ticket = Ticket(info)
                    data = {
                        "Device": self._device,
                        "Version": self._version,
                        "Key": self._key,
                        "h_orgtk_wct_no": ticket.sale_info1,
                        "h_orgtk_ret_sale_dt": ticket.sale_info2,
                        "h_orgtk_sale_sqno": ticket.sale_info3,
                        "h_orgtk_ret_pwd": ticket.sale_info4,
                    }
                    r = self._session.get(API_ENDPOINTS["myticketseat"], params=data)
                    j = json.loads(r.text)
                    if self._result_check(j):
                        seat = (
                            j.get("ticket_infos", {})
                            .get("ticket_info", [{}])[0]
                            .get("tk_seat_info", [{}])[0]
                        )
                        ticket.seat_no = seat.get("h_seat_no")
                        ticket.seat_no_end = None
                    tickets.append(ticket)
                return tickets
        except NoResultsError:
            return []

    def reservations(self, rsv_id=None):
        data = {
            "Device": self._device,
            "Version": self._version,
            "Key": self._key,
        }
        r = self._session.get(API_ENDPOINTS["myreservationview"], params=data)
        self._log(r.text)
        j = json.loads(r.text)
        try:
            if not self._result_check(j):
                return []

            jrny_info = j.get("jrny_infos", {}).get("jrny_info", [])
            reserves = []

            for info in jrny_info:
                train_info = info.get("train_infos", {}).get("train_info", [])
                for tinfo in train_info:
                    reservation = Reservation(tinfo)
                    reservation.tickets, reservation.wct_no = self.ticket_info(
                        reservation.rsv_id
                    )
                    if rsv_id and reservation.rsv_id == rsv_id:
                        return reservation
                    reserves.append(reservation)
            return reserves

        except NoResultsError:
            return []

    def ticket_info(self, rsv_id=None):
        data = {
            "Device": self._device,
            "Version": self._version,
            "Key": self._key,
            "hidPnrNo": rsv_id,
        }
        r = self._session.get(API_ENDPOINTS["myreservationlist"], params=data)
        self._log(r.text)
        j = json.loads(r.text)
        # 항상 (좌석 목록, wct_no) 를 돌려준다. 예전처럼 None 을 돌려주면 받는 쪽
        # (reservations)의 언패킹이 터져서, 서버에선 이미 잡힌 예약이 예외로 끝나
        # '예약 실패'로 보이고 다시 예약하게 된다 (중복 예매). 예약대기처럼 좌석이
        # 아직 없는 예약에서 이 경로를 탄다.
        try:
            if not self._result_check(j):
                return [], None

            wct_no = j.get("h_wct_no")
            if jrny_info := j.get("jrny_infos", {}).get("jrny_info", []):
                if seat_info := jrny_info[0].get("seat_infos", {}).get("seat_info", []):
                    return [Seat(seat) for seat in seat_info], wct_no
            return [], wct_no

        except NoResultsError:
            return [], None

    def pay_with_card(
        self,
        rsv,
        card_number,
        card_password,
        birthday,
        card_expire,
        installment=0,
        card_type="J",
    ):
        if not isinstance(rsv, Reservation):
            raise TypeError("rsv must be a Reservation instance")

        data = {
            "Device": self._device,
            "Version": self._version,
            "Key": self._key,
            "hidPnrNo": rsv.rsv_id,
            "hidWctNo": rsv.wct_no,
            "hidTmpJobSqno1": "000000",
            "hidTmpJobSqno2": "000000",
            "hidRsvChgNo": "000",
            "hidInrecmnsGridcnt": "1",
            "hidStlMnsSqno1": "1",
            "hidStlMnsCd1": "02",
            "hidMnsStlAmt1": str(rsv.price),
            "hidCrdInpWayCd1": "@",
            "hidStlCrCrdNo1": card_number,
            "hidVanPwd1": card_password,
            "hidCrdVlidTrm1": card_expire,
            "hidIsmtMnthNum1": installment,
            "hidAthnDvCd1": card_type,
            "hidAthnVal1": birthday,
            "hiduserYn": "Y",
        }

        r = self._session.post(API_ENDPOINTS["pay"], data=data)
        self._log(r.text)
        j = json.loads(r.text)
        if self._result_check(j):
            return True
        return False

    def cancel(self, rsv):
        if not isinstance(rsv, Reservation):
            raise TypeError("rsv must be a Reservation instance")
        data = {
            "Device": self._device,
            "Version": self._version,
            "Key": self._key,
            "txtPnrNo": rsv.rsv_id,
            "txtJrnySqno": rsv.journey_no,
            "txtJrnyCnt": rsv.journey_cnt,
            "hidRsvChgNo": rsv.rsv_chg_no,
        }
        r = self._session.post(API_ENDPOINTS["cancel"], data=data)
        self._log(r.text)
        j = json.loads(r.text)
        return self._result_check(j)

    def refund(self, ticket):
        data = {
            "Device": self._device,
            "Version": self._version,
            "Key": self._key,
            "txtPrnNo": ticket.pnr_no,
            "h_orgtk_sale_dt": ticket.sale_info2,
            "h_orgtk_sale_wct_no": ticket.sale_info1,
            "h_orgtk_sale_sqno": ticket.sale_info3,
            "h_orgtk_ret_pwd": ticket.sale_info4,
            "h_mlg_stl": "N",
            "tk_ret_tms_dv_cd": "21",
            "trnNo": ticket.train_no,
            "pbpAcepTgtFlg": "N",
            "latitude": "",
            "longitude": "",
        }
        r = self._session.post(API_ENDPOINTS["refund"], data=data)
        self._log(r.text)
        j = json.loads(r.text)
        return self._result_check(j)
