"""
The Credentials block every SFA endpoint expects.

SFA's API is uniform: each request is {"Credentials": {...}, "RequestData": {...}},
and the Credentials half is the same fixed set of keys whichever service is being
called. It is their mobile app's device/session envelope — DeviceID, IMEI, Latitude
and friends — none of which means anything server-to-server, so almost every field
stays at a fixed default.

Three carry meaning for us:

    ServiceName   which endpoint this is. SFA reads it; it must match the URL.
    CompanyID     the client's company, when the caller has one to send.
    LoginUserID   the SFA user the action is attributed to.

Shared by app/lizo/notify.py (SaveWhatsAppOrderStatus) and app/lizo/approve.py
(ApproveOrder) so the fixed keys are declared once. A third SFA call added later
gets the same block for free.
"""

# The endpoint names, matching the last path segment of each SFA URL.
SERVICE_SAVE_STATUS   = "SaveWhatsAppOrderStatus"
SERVICE_APPROVE_ORDER = "ApproveOrder"


def credentials(service_name: str, company_id: int = 0, login_user_id: str = "") -> dict:
    """
    SFA's fixed Credentials block, with the three meaningful fields filled in.

    Rebuilt per call rather than shared as a module constant, so a caller mutating
    the dict it gets back cannot corrupt it for everyone else.

    The defaults are what the status callback has always sent, so notify.py's payload
    is unchanged by moving here.
    """
    return {
        "CheckSum": 0,
        "Operation": 0,
        "Latitude": "",
        "Longitude": "",
        "Altitude": "",
        "DeviceID": "",
        "IMEI": "",
        "LoginUserID": login_user_id,
        "ServiceName": service_name,
        "TokenID": "",
        "BluetoothID": "",
        "IsZipped": 0,
        "CompanyID": company_id,
        "SendStatus": 0,
        "ApkType": "",
        "DeviceNotificationID": "",
        "HierarchyTypeID": "",
        "HierarchyID": "",
    }
