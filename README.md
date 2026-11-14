# Greenlight

PTO request app. Streamlit hosts the HTML front end and talks to MongoDB.

## Files

| File | What it is |
| --- | --- |
| `app.py` | Streamlit app. Reads and writes MongoDB, checks the admin passcode, decides each request. |
| `greenlight_rules.py` | The approval rules, kept separate so they can be tested. |
| `greenlight_component/index.html` | The whole front end (one HTML file). |
| `roster_template.xlsx` | Formatted roster template with dropdowns and a how-to tab. The app offers the same file in Settings > Roster. |
| `.streamlit/secrets.toml.example` | The secrets the app expects. |

## Run it locally

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then fill in your values
streamlit run app.py
```

Open the address Streamlit prints (usually http://localhost:8501). `greenlight_component/index.html` is the app's screen and talks to `app.py`, so it only saves data when opened through Streamlit. If you double-click it on its own, it opens as a preview with sample data and a note saying nothing is saved.

## Deploy on Streamlit Community Cloud

1. Push this folder to a GitHub repository. Do not commit `.streamlit/secrets.toml`.
2. Create a new app that points at `app.py`.
3. In App settings > Secrets, paste:

```toml
MONGODB_URI = "mongodb+srv://USER:PASSWORD@YOUR-CLUSTER.mongodb.net/?retryWrites=true&w=majority"
ADMIN_PASSWORD = "choose-a-strong-passcode"        # WFM role
OVERRIDE_PASSWORD = "choose-another-passcode"       # Admin role (decides override requests)
MONGODB_DB = "greenlight"      # optional
```

4. In MongoDB Atlas, allow the app to connect under Network Access.

The database, collections and indexes are created on first run.

## First-time setup in the app

1. Pick **WFM** on the landing page and enter `ADMIN_PASSWORD`. **PTO Dashboard** (with coverage charts) and **Settings** appear only after that. Change the passcode any time in Settings > WFM passcode. Override requests are decided by the separate **Admin** role, which signs in with `OVERRIDE_PASSWORD`.
2. Settings > Roster: download the template, fill it in, upload it. Columns: Agent Name, Current Manager, Region, Department, Work Days, Rest Days, Shift. The upload takes Excel (.xlsx, first sheet) or CSV. There is no employee ID: agents are matched by name, so each name should appear once (a repeated name is skipped). An agent who is already on the roster keeps their requests when you upload again. Work Days and Rest Days each take a range (`Mon-Fri`) or a list (`Tue,Wed,Thu,Fri,Sat`). Fill in either one and the other is worked out; if both are filled, Work Days is used; if both are blank, Mon-Fri is assumed. Shift is the start and end time in Manila time (PHT, UTC+8), for example `09:00-18:00`, whatever the app timezone is set to; overnight shifts such as `21:00-06:00` are fine. Each agent's Region decides which region's hourly coverage they count toward, so the coverage rows follow whatever roster you upload.
3. Settings > PTO allocation: select a department tile. A window opens with one calendar per region. Type the default slots per day, choose all regions or one, and All months or a month and year, then select Apply. Adjust single days on the calendars. Or choose **Required headcount per day** in the same bar: enter how many agents must stay on duty, and Greenlight works out each day's slots as agents scheduled that day (from the roster's work days) minus that number, never below 0. Days typed by hand still win. **Stop auto** puts the region back on fixed slots, and applying fixed slots for the same months also replaces it. Each day shows S (agents scheduled to work) and HC (headcount left if every PTO slot is taken), which updates as you type. The window shows one region at a time; use the arrows, the region chips or the left and right arrow keys to switch. Above the region's calendar, an hourly coverage table covers the whole month: one row per hour the region actually works, and two columns under each date: Sched (agents scheduled that hour) and After (agents left if every PTO slot that day is taken).
4. Settings > Approval rules: choose Automatic or Manual review, and the minimum notice.

## How a request is decided

For each date, in this order:

1. The person already has an approved or pending request for that date: skipped.
2. Manual review is on: saved as pending. It shows in the admin **Pending** tab, sorted by date, in the "Short notice" column if it was sent later than the minimum notice.
3. Automatic, and the date is sooner than the minimum notice: rejected.
4. Automatic: approved if approved requests for that department, region and day are below the allocation. Otherwise it is recorded as rejected, so an admin can still override it.

Allocation for a day is the day's own number if one was set; else, if a required headcount is set, the agents scheduled that day minus the required headcount (never below 0); else that month's default, else the every-month default, else 0.
An Admin override (decided by the Admin role) approves a rejected request even when the day is full, and stores the reason.

Override requests: when a picked date is inside the notice window or has no slot left, the agent can select **Request override** and give a reason. In automatic mode the date is still rejected right away, but it is recorded with the reason. Every open override request goes to the admin **Override requests** tab, split into **Short notice** and **No allocation**, filtered by month and year. Admins can approve or reject one, select several and decide them together, or reject all shown. Approving asks for one reason, which is saved on each request.

## PTO report

**Extract report** downloads an Excel file that matches the screen it was opened from: the PTO Dashboard (WFM and managers; a manager's report follows the My team / All requests switch) or Reports (Admin). It covers the month, department and region chosen at the top. It is built in the browser, so it needs no extra server setup. Tabs:

- **Dashboard**: WFM and Admin get headcount after PTO first (average headcount after PTO, coverage kept, lowest day, days below required), then approved, rejected and pending; managers get approved, rejected, pending and busiest day. Then requests by every status (approved, rejected, waiting for manager approval, waiting for override approval, pending review, cancelled), requests by region, override requests by type and outcome (WFM and Admin), and charts: daily headcount after PTO (WFM and Admin), approved and rejected per day, requests by status, requests by region.
- **Requests**: one row per request with where it stands (including the two waiting steps), the manager reply deadline, filters and a frozen header. WFM and Admin also get the override request columns.
- **Daily**: slots, approved, rejected and waiting for each day, with formulas for slots left and totals. WFM and Admin also get scheduled, headcount after PTO (a formula) and required.
- **Coverage** (WFM and Admin): every scheduled day, lowest coverage first, with scheduled, on PTO, waiting, headcount after PTO, required, coverage % and an OK / Tight / Below required status.
- **By team**: each department and region, with slots used as a percentage

## Data in MongoDB

- `roster`: name, manager, region, department, work_days, shift, plus a hidden internal key in `employee_id` (the lower-cased name for new agents; agents added before keep their old key). It is never shown in the app
- `requests`: one document per person per date (with a `notices` list of messages for the agent and `mgr_reminded` once the day-before reminder is sent), with status (`awaiting`, `pending`, `approved`, `rejected`, `cancelled`), mgr_deadline, mgr_status (`approved`, `rejected`, `expired`), mgr_by, decided_at, cancelled_from, reason, short_notice, decided_by, override_reason (admin), override_request (agent's reason), override_status (`open`, `approved`, `declined`), override_type (`notice` or `allocation`), note, submitted_at
- `allocations`: `kind: "default"` (key `*` or `YYYY-MM`, slots per day), `kind: "required"` (key `*` or `YYYY-MM`, agents needed on duty, stored in `slots`) and `kind: "day"` (key `YYYY-MM-DD`), each with department and region
- `meta`: one document, `_id: "version"`, a counter that goes up on every change so open pages can check for updates cheaply
- `managers`: one document per manager (`_id` is the name as written in the roster) with a hashed `passcode`
- `sessions`: one document per signed-in manager or WFM browser (`_id` is a hash of the sign-in token), removed automatically after 30 days of no use or when the person signs out
- `locks`: short-lived documents (`_id` is department|region|date) that let only one person at a time take a slot for one team on one day. Each lives a few milliseconds and is removed automatically if a server crashes mid-save.
- `settings`: one document, `_id: "config"`, with approval_mode, min_notice_days, mgr_max_hours

## Who sees what

Every role has **Guide** as the first tab. The guide also opens after each sign in, with a **Get started** button that goes to the dashboard.

The app opens on a landing page with four circles: **GSD** (agents), **Manager**, **WFM** and **Admin**. People sign in first (agents pick their name, Manager, WFM and Admin enter a passcode). Every time someone signs in, the role's short welcome guide is the first thing shown, with a Get started button that leads to their view. (Reloading while still signed in goes straight to the view.) Guide is always the first tab, so it can be reopened at any time. Click the Greenlight logo to return to the landing page. Every step of every guide (GSD, Manager, WFM and Admin) has a short animated demo recorded from the current app. When a role is picked, its circle moves to the centre and the others fade away. Manager opens a window of profile circles with the names from the roster's Current Manager column; pick yours, then enter the passcode. WFM asks for the WFM passcode and Admin for the Admin passcode (developers use their key in either). The GSD, Manager and WFM guides highlight the manager approval window (72 hours by default, set by WFM).

**Stay signed in.** Managers and WFM stay signed in on that browser until they press Sign out (a sign-in token is kept in the browser and checked against the `sessions` collection, for up to 30 days of use). Agents are remembered on their device too: the app opens straight to their calendar, and Sign out (or Not you?) forgets them. Changing the admin passcode signs out other admin devices.

One app address for everyone. People sign in only when their role needs it.

| Role | How they get in | What they see |
|---|---|---|
| Agent | No passcode. Search your name once; the device remembers you until Sign out. | PTO Dashboard: Submit PTO request circle and request history. Tap a requested date to see its timeline (Submitted, Manager approval, Automated WFM review), edit it while the manager has not answered, or cancel it before the date. Plus the dashboard. |
| Manager | Pick Manager, choose your profile circle, enter your passcode. Stays signed in. | PTO Dashboard (own team by default, All requests on a switch), plus **Approvals**: only their own team's requests, with bulk approve and reject, and a list of upcoming approved time off (only the agent can cancel a request). |
| Admin | Pick Admin, enter `OVERRIDE_PASSWORD`. Stays signed in. | Override requests: every override request from every agent goes here, to approve or reject one by one or in bulk. Reports: daily headcount after PTO (chart and tables), lowest-coverage days, requests by status, PTO days by team and override outcomes, filtered by month, department and region. |
| WFM | Pick WFM, enter the WFM passcode. Stays signed in. | PTO Dashboard (headcount after PTO and coverage charts, requests by status, PTO by team, overrides, people off each day, and the approved/rejected table, where selecting a row opens the request to edit or fix it), Pending (only while Manual review is on), Settings, roster, allocation, manager passcodes. |
| Developer | Same sign-in with `DEV_PASSWORD`. | Same as WFM, and the key keeps working if the admin passcode is lost. The header shows Developer. |

Manager passcodes are set by WFM in Settings > Manager sign-in (hashed in the `managers` collection). Names come from the Current Manager column of the roster.

## How a request moves

1. The agent submits and a pop-up shows the journey: Submitted, Manager approval, Automated WFM review, with the manager's deadline. If the date has an open slot and enough time is left, the request is **Awaiting manager** and the slot is held, so nobody else can take it.
2. The manager has one approval window, 72 hours by default, counted from when the request is sent. WFM can change it in Settings > Approval rules, next to the minimum notice (`mgr_max_hours`).
3. Only the agent's own manager can answer this manager step (WFM and Admin cannot answer it for them). Manager approves: the usual rules then decide (minimum notice, allocation, automatic or manual mode), so the result is Approved, Pending (manual mode) or Rejected.
4. Manager rejects, or does not answer before the deadline: the request is rejected and the slot goes back. Unanswered requests are rejected automatically within about 20 seconds of the deadline while anyone has the app open, and are also swept the next time anyone opens it.
5. Short-notice dates and full days skip the manager step and follow the existing override flow.
6. Agents tap the date on their calendar to follow the request. From there they can edit it (date or note) until the manager answers, and can cancel an awaiting, pending or approved request before its date. Managers and WFM can cancel approved time off. A cancelled request is kept for the record and its slot opens up again.

## Notifications for agents

Agents see a bell next to their name on the PTO Dashboard, with a count of new messages, and a short pop-up when something new arrives. They are told when:

- their manager has not answered a day before the approval window ends (the request is rejected automatically at the deadline);
- a request was rejected because the manager did not answer in time;
- the Admin approved or rejected their override request;
- an override request was still not reviewed on the day before the requested date, so the system rejected it automatically that day (the message names the date it was closed and says the date is denied).

These checks run whenever anyone has the app open (the keep-awake job in `.github/workflows` keeps that frequent). Messages are stored on the request in a `notices` list.

## Override requests when submitting

When an agent picks several dates and some need an override (short notice or no slot left), each of those dates gets its own required reason field, and the button becomes **Submit request and override request**. One click sends everything: the other dates go in as normal requests and only the override dates go to the Admin queue. Only the agent can cancel their own request.

## Many people at once

- Many agents can submit at the same moment. For each team and day, the slot check and the save happen one person at a time (a lock in the `locks` collection), so two people can never both take the last slot, and one person clicking twice or submitting from two tabs gets one request, not two. Other teams and days never wait.
- The same lock covers a manager approval (the allocation is re-checked when the manager says yes) and an agent moving a request to another date.
- Writes use MongoDB's majority write concern with retries, so a saved request survives a cluster failover or a brief network blip.
- A request that a manager answers in the same moment it expires is applied once: whichever lands first wins, and the other gets "already answered".

## Keep the app awake

Streamlit Community Cloud puts apps to sleep after about 12 hours without visitors. The workflow in `.github/workflows/keep-awake.yml` opens the app in a browser every 6 hours and wakes it if it is asleep. To use it, put this folder in a GitHub repo, then add a repository variable named `APP_URL` (Settings > Secrets and variables > Actions > Variables) with your app address. Run it once from the Actions tab to check it. A plain uptime pinger (a simple HTTP check) does not stop the sleep, because Streamlit only counts a real browser session.

## Good to know

- Roster shift times are always entered in Manila time (UTC+8). Settings > Timezone sets the app timezone (Manila by default). Greenlight converts shift times to it for the hourly coverage table, schedules, the roster table and reports, and uses it for "today", the minimum notice and submitted times. Daylight saving is handled for each date, so a London or New York table moves by an hour when the clocks change. Changing the timezone never edits the stored roster.

- The calendar checks for changes every 4 seconds (a tiny read of one `meta` counter document; data reloads only when something changed) while the tab is open, so approvals made by other people show up without a reload. Change `POLL_MS` in `index.html` to adjust.
- The admin passcode is one shared secret. Admin actions are checked on the server, not only hidden in the page.
- `DEV_PASSWORD` in secrets is a developer key for maintenance. It always signs in with full admin access, even after an admin changes the passcode, and the header shows **Developer**. It also works as the current passcode in Settings > Admin passcode, so you can reset a forgotten one. It is never stored in MongoDB.
- `ADMIN_PASSWORD` in secrets is the first passcode. Once it is changed in Settings, the new one is stored hashed in MongoDB (`settings.config.admin_password`) and the secret is no longer used. Forgot it? Delete that field in MongoDB and the secret works again.
- Excel upload uses the SheetJS library loaded from cdnjs, so the browser needs internet access for it. CSV upload works without it.
- Requests older than 400 days stay in MongoDB but are not loaded into the app.
