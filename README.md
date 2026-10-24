# Track 2: Customer service chatbot with bookings

**Client (fictional):** Kallang Auto Care, a car servicing workshop in Singapore.

## The problem

Kallang Auto Care's service desk is two people. Most calls are the same questions (opening hours, prices, what a servicing package includes) or customers wanting to book a slot. Calls pile up at 8:30am and during lunch, and customers hang up.

They want a chat assistant that can:

1. **Answer questions** from their knowledge base, and say "I don't know" when the answer isn't there.
2. **Take a booking** over several turns, collecting service, plate number, date, time, name, mobile and email, with consent, and saving it.
3. **Pass to a human** when a customer complains or the bot gets stuck.

## The design you must follow

One message at a time goes through a **router**. The router is the only part that decides *where* a message goes. The booking itself is **not** left to the model: it is a step-by-step flow written in code.

```
                         user message
                              |
                  [guardrail: injection? NRIC? card?]   <- code
                              |
                   [ROUTER: LLM classifies]             <- model
          ________________|__________________________________
         |                |                    |             |
        faq            booking              handoff     out_of_scope
         |                |                    |             |
  TF-IDF retrieval   BOOKING FLOW (code)   "passing you    polite
  over faq_kb.md     service -> plate ->    to a service   redirect
         |           date -> time -> name   advisor"
  LLM answers ONLY   -> mobile -> email ->
  from retrieved     consent -> confirm
  sections + cites   -> save to bookings.csv
         |                |
         +--> if a booking is in progress, answer the question,
              then repeat the current booking step
```

**Intents the router returns:** `faq`, `booking`, `handoff`, `out_of_scope`.

**Booking states:** `service`, `vehicle_plate`, `date`, `time_slot`, `name`, `mobile`, `email`, `consent`, `confirm`, then `done`. Outside a booking the state is `idle`.

The full rules for each step are in `docs/booking_flow_spec.md`. Read it before you brief Claude Code.

**Why mix a model with a fixed flow?** The model is good at language: understanding "next Tues afternoon" or noticing a question in the middle of a booking. Code is good at rules: whether 28 Oct is fully booked, whether "1234 5678" is a valid mobile number, whether a booking is saved. A business can't let a model "decide" a booking.

## Your data

| File | What it is |
| --- | --- |
| `data/faq_kb.md` | The knowledge base: 12 sections, one per `##` heading |
| `data/services.csv` | 8 services with price, duration and bay type (`general`, `ev`, `detailing`) |
| `data/availability.csv` | Free and taken slots, Mon–Sat 26 Oct to 7 Nov 2026, by bay type |
| `data/bookings.csv` | Empty except for the header; your flow appends confirmed bookings here |
| `docs/booking_flow_spec.md` | States, prompts, validation rules, consent text, reference format |
| `test_set/conversations.jsonl` | 14 scripted conversations with the expected route and state for every turn |

"Today" for this project is fixed at **Saturday 24 Oct 2026**, so "next Tuesday" always means 27 Oct.

## Must have by 12:30 Saturday

- [ ] Router sends a message to `faq` or `booking` correctly
- [ ] FAQ answers from the knowledge base and names its source section
- [ ] Booking flow runs end to end for the happy path (test C01) and saves a row with a reference number
- [ ] At least the mobile and plate steps reject bad input

## Then add (in this order)

1. Consent step and the "never collect NRIC or card numbers" rule
2. A question in the middle of a booking is answered, then the booking resumes (test C04)
3. Cancel, start over, and "actually change the date" (tests C07, C08)
4. Handoff for complaints, and after 3 failed attempts at one step
5. The synonym trap: customers say "maintenance", the knowledge base says "servicing package" (test C03)
6. Streamlit page showing the chat, the current booking step and a masked trace
7. Measure it: tokens, cost and seconds per conversation, before and after one improvement

## How the judges will test it

- They will run the flow live with **3 conversations you have not seen**, including things like changing an answer, giving a bad value several times, and offering personal data the bot should not keep.
- They will look at your **trace** to see which route each message took.
- They will check `bookings.csv`: the right booking is saved, and nothing is saved when it shouldn't be.
- They will try to talk the bot into a discount.

## PII rules (non-negotiable)

- Ask for consent before saving anything. No consent, nothing saved.
- Never store or log NRIC, FIN, passport or card numbers. If a customer offers one, remove it and tell them it isn't needed.
- Validate the plate, mobile and email with code (regex), not with the model, so this data isn't sent to the model at all.
- Mask PII in anything you log or show in the trace.

## Data provenance

All data in this track is **synthetic**, created on 18 Oct 2026 for SMU Academy PCML Module 6. Kallang Auto Care is a fictional business: its address, prices, policies, availability and customers are invented. Names, plate numbers, NRIC-format and card-format numbers in the test conversations are made up for testing only, and email addresses use the reserved `example.com` domain. Any resemblance to real businesses or people is coincidental.
