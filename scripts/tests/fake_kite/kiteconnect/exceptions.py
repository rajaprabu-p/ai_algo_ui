"""Exception names the strategy imports from kiteconnect.exceptions."""


class KiteException(Exception):
    pass


class PermissionException(KiteException):
    pass


class InputException(KiteException):
    pass


class DataException(KiteException):
    pass


class TokenException(KiteException):
    pass


class NetworkException(KiteException):
    pass


class GeneralException(KiteException):
    pass


class OrderException(KiteException):
    pass
