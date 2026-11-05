import os

os.environ.setdefault("INITIALIZE_APP", "false")

import main


def _tcs_payload():
    return {
        "isSuccess": True,
        "responseData": {
            "message": "SUCCESS",
            "shipmentinfo": [{"consignee": "Tahir Ikram Rathore", "destination": "Jalalpur Jattan"}],
            "checkpoints": [
                {"datetime": "Wednesday Nov 4, 2026 22:49", "status": "Arrived at TCS Facility", "recievedby": "LAHORE"},
                {"datetime": "Wednesday Nov 4, 2026 22:03", "status": "Shipment Picked Up", "recievedby": "LAHORE"},
            ],
        },
    }


def test_tcs_history_is_oldest_to_newest_and_maps_to_dispatched():
    history = main.tcs_payload_to_tracking_history("173019564532", _tcs_payload())

    assert [event["ProcessDescForPortal"] for event in history] == ["Shipment Picked Up", "Arrived at TCS Facility"]
    assert history[-1]["ConsigneeCity"] == "Jalalpur Jattan"
    assert main.summarize_tracking_result("173019564532", history)["status"] == "Dispatched"


def test_tcs_tracking_number_and_courier_are_recognized():
    assert main.is_tcs_tracking("173019564532")
    assert main.courier_label_for_tracking("", "173019564532") == "TCS"
    assert main.normalize_status_bucket("Arrived at TCS Facility") == "Dispatched"
