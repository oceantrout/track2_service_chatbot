# Booking flow specification: Kallang Auto Care

The booking path is a **deterministic state machine written in code**. The language model may only help to *extract* a value from free text (for example "next Tues afternoon" → a date and a time). Your code validates every value and decides what happens next. The model never decides whether a booking is valid, and never writes to `bookings.csv`.

Config: "today" is fixed at **2026-10-24 (Saturday)** so that test conversations give the same answers every time.

## States, in order

`service → vehicle_plate → date → time_slot → name → mobile → email → consent → confirm → done`

Outside a booking the state is `idle`. After a confirmed booking it is `done`.

Simple rule that makes the flow robust: **the current state is the first field that is still empty.** Changing or clearing an earlier field automatically sends the flow back to it.

| State | Bot asks | Who extracts | Validation (code) |
| --- | --- | --- | --- |
| service | "Which service would you like?" plus the list from `services.csv` with prices | LLM may map free text to a `service_code` | Must be a code in `services.csv` |
| vehicle_plate | "What is your vehicle plate number? (for example SBA1234K)" | **Regex only** | Uppercase, remove spaces, must match `^S[A-Z]{1,2}\d{1,4}[A-Z]$` |
| date | "Which date? We take bookings Mon–Sat from 26 Oct to 7 Nov 2026." | LLM may turn "next Tuesday" into `YYYY-MM-DD` | Inside the booking window; not a Sunday; at least one free slot for the service's `bay_type` |
| time_slot | "Available times on <date>: 09:00, 10:00 … Which time suits you?" | LLM may turn "2pm" or "afternoon" into `HH:MM` | Must be a free slot in `availability.csv` for that date and bay type, and not already in `bookings.csv`. "morning" = earliest free before 12:00, "afternoon" = earliest free from 12:00 |
| name | "What name should the booking be under?" | **Code only** (strip "my name is …") | 2–60 characters, letters, spaces, `' - / . @` |
| mobile | "What is your mobile number? (8-digit Singapore number)" | **Regex only** | Remove spaces and a leading +65; must match `^[89]\d{7}$` |
| email | "What email address should we send the confirmation to?" | **Regex only** | Standard email pattern |
| consent | The consent text below | Code | yes → continue; no → discard everything, outcome `no_consent` |
| confirm | Summary of all fields, then "Shall I confirm?" | Code | yes → save and give a reference; anything else → ask what to change |

Why regex for PII: the plate number, mobile number and email are personal data. A regular expression handles them perfectly, so there is no reason to send them to an external model. This is data minimisation, one of the ideas behind Singapore's PDPA.

## Retry rule

Each state allows **3 failed attempts**. After the third failure, stop the booking, offer the service desk, and end with outcome `handoff`. Never loop forever.

When a value fails validation, say what was wrong and ask again (for example "That doesn't look like a valid Singapore mobile number").

## Commands that work at any step

| User says | What happens |
| --- | --- |
| "cancel", "stop", "never mind" | Discard all fields, state `idle`, outcome `cancelled`. Nothing is saved. |
| "start over", "restart" | Clear all fields, go back to `service`. |
| "actually change the date to 3 Nov" | Detect *change* + a field name; validate the new value; set it. Changing the **date** clears the time slot. Changing the **service** to a different bay type clears the time slot. The flow then continues at the first empty field. |
| A question, e.g. "do you accept PayNow?" | The router sends it to the FAQ path. Answer it, then repeat the current booking question. The booking is not lost. |

## Never collect

- **NRIC, FIN or passport numbers.** If the user offers one, remove it before processing, never store or log it, and say it isn't needed.
- **Payment card numbers.** Same rule. We never take payment in chat.

## Consent text (state: consent)

> Kallang Auto Care will use your name, mobile number, email and plate number only to manage this booking, send reminders and keep your service record (kept 2 years after your last visit). Do you agree? (yes / no)

## Confirmation summary (state: confirm)

> Please check: Basic servicing package (SGD 128) for SBA1234K on Tue 27 Oct 2026 at 10:00. Name: Tan Wei Ming, mobile: 91234567, email: weiming.tan@example.com. Shall I confirm? (yes, or tell me what to change)

## Saving a booking

Append one row to `data/bookings.csv`:

`booking_ref, created_at, service_code, vehicle_plate, appointment_date, appointment_time, name, mobile, email, consent_given_at`

**Booking reference format:** `KAC-YYMMDD-NNN`, where `YYMMDD` is the appointment date and `NNN` is the booking's sequence number for that date, starting at 001. Example: `KAC-261027-001`.

A saved booking takes the slot: later customers must not be offered the same date, time and bay type.

## Logging

Keep a trace of every turn (route, state before and after, FAQ section cited) so judges can see your workflow. **Mask PII in the trace**: mobile as `9*** 4567`, email as `w***@example.com`, plate as `S****K`, and NRIC or card numbers removed completely.

## Simplifications (fine for the hackathon)

- One booking per bay type per start time; service duration is for information only.
- Only private-car plates starting with S are accepted, and the plate's check letter is not verified.
- The bot takes new bookings only. Changing or cancelling an *existing* booking is handled by the service desk (the FAQ explains how).
