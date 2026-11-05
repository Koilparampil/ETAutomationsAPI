# 14 — Map from the original code to the new program

Use this to check that nothing was dropped. "Browser" = Playwright in the original.

| Original file / function | What it did | Browser? | New home | Spec |
|---|---|---|---|---|
| `mainIG.py` `get_data` | Read `doc,amount` CSV (`utf-8-sig`) | no | `pipeline.read_input`, `rules.parse_row` | 01 §2 |
| `mainIG.py` `main` (branching) | Suffix rules, thresholds, routing, error → file mapping | no | `rules.py`, `pipeline.py` | 01 §3–14, 10 |
| `mainIG.py` QBO login block | Persistent Chromium profile, account chooser, password fill, wait for `/app/homepage` | **yes** | removed — OAuth refresh token only | 04 |
| `mainIG.py` `write2*File` | Append to Send2*.txt / Failed / Customs files | no | `outputs.OutputWriter` | 10 §5 |
| `tinkyWinky.py` `get_user_inputs_authed` | Tk file picker | no | `ui.pick_input_file` | 11 §6 |
| `tinkyWinky.py` `get_user_inputs`, `tinkyWinky_log_only.py` | Tk credential prompts | no | removed — credentials from `.env` | 02 |
| `carrierTing/carriers.py` `carrierIDthenETAcheck` | Pattern → carrier, ETA lookup, 6-business-day window | (via carriers) | `carriers/router.py`, `business_days.py`, `rules.in_window` | 01 §4–5 |
| `MSC/signInMSC.py` | Headless login to myMSC, save storage state, GET access token | **yes** | `carriers/msc.py` auth | 06 |
| `MSC/checkETA.py` `checkingMSC` | New browser per booking + GraphQL POST + event selection + customs-hold rule | **yes** | `carriers/msc.py` | 06 |
| `Maersk/checkETA.py` `checkingMaersk` | Headful Chrome per booking, Akamai warm-up, scrape new/old tracking UI | **yes** | `carriers/maersk.py` (official API) | 07 |
| `CMA/checkETA.py` `checkingCMA` | Headful Chrome per booking, DataDome, form POST, read `options.responseData`/`model` | **yes** | `carriers/cma.py` (official API) | 08 |
| (Grimaldi) | Not implemented | — | `carriers/grimaldi.py` stub / aggregator | 09 |
| `QBTings/apiREST.py` `get_access_token` | OAuth refresh, write rotated token to `.env` | no | `qbo/auth.QboAuth` | 04 |
| `QBTings/apiREST.py` `get_invoice_by_number` | `SELECT * FROM Invoice WHERE DocNumber = '…'` | no | `QboClient.find_invoices_by_doc_numbers` (batched) | 04 |
| `QBTings/playWrightQB.py` | Locator helpers, login wait | **yes** | removed | — |
| `QBTings/invoiceProcessing.py` `process_invoice` | Read balance/partial banner, DocNumber `A`, ETA field, Review and send, subject, Send | **yes** | `QboClient.update_invoice` + `send_invoice` (+ `qbo/email.py`), decisions in `pipeline.py` | 04, 01 §6.3–6.4 |
| `QBTings/invoiceProcessing.py` `process_future_invoice` | ETA field + Save | **yes** | `QboClient.update_invoice` | 04, 01 §6.5 |
| `stringTing/subjectEdit.py` `subjectDecision` | Subject text | no | `texts.subject_for` | 01 §8 |
| `VShip/syncSignInVShipCRM.py` | Login → token file | no | `vship/client.VShipAuth` (TokenStore) | 05 |
| `VShip/bookingLookUp.py` | Search + best match | no | `VShipClient.find_booking_id`, `vship/logic.best_match` | 05 §6 |
| `VShip/customerLookUp.py` `lookup_customer_notif` | Notif #1/#2 status + 7-day rule | no | `vship/logic.notif_status` | 01 §11 |
| `VShip/customerLookUp.py` `lookup_customer_name` | Customer name for Bindu | no | `VShipClient.get_customer_name` | 05 |
| `VShip/noteSearch.py` `lookup_TR_SWB` | TR/SWB keyword match on internal comments | no | `vship/logic.has_tr_swb_note` | 01 §12 |
| `VShip/writeNotif.py` `note_writing` | ARRIVED/ARRIVING comment | no | `texts.vship_comment_for` + `VShipClient.post_comment` | 01 §10, 05 |
| `VShip/booking_changes.py` `make_vship_booking_changes` | PATCH expected arrival | no | `VShipClient.patch_expected_arrival` | 05 |
| `requirements.txt` | playwright, pytest-playwright, o365, msal, numpy, pandas, requests, bs4… | — | `pyproject.toml`: httpx, python-dotenv, pandas (+ optional extras) | 03 §2 |
| Cached files `auth_for_MSC.json`, `MSCauthToken.json`, `auth_for_VshipCRM.txt`, `auth_for_CMA.json`, QBO browser profile | Sessions/tokens next to the script or in `%LOCALAPPDATA%` | — | one `tokens.json` in `ETA_STATE_DIR` (0600) | 03 §7 |

Environment variables that become obsolete: `QB` (QBO web password), `QB_HEADLESS`, `HOME_PAGE`
(and `LOGIN_URL` unless 06 keeps it as a legacy alias). See 02-configuration.md for the full list.
