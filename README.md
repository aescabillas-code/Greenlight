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

## Deploy on Streamlit Community Cloud

1. Push this folder to a GitHub repository. Do not commit `.streamlit/secrets.toml`.
2. Create a new app that points at `app.py`.
3. In App settings > Secrets, paste:

```toml
MONGODB_URI = "mongodb+srv://USER:PASSWORD@YOUR-CLUSTER.mongodb.net/?retryWrites=true&w=majority"
ADMIN_PASSWORD = "choose-a-strong-passcode"
MONGODB_DB = "greenlight"      # optional
```

4. In MongoDB Atlas, allow the app to connect under Network Access.

The database, collections and indexes are created on first run.

## First-time setup in the app

1. Select **Admin sign in** and enter `ADMIN_PASSWORD`. **Requests** and **Settings** appear only after that. Change the passcode any time in Settings > Admin passcode.
2. Settings > Roster: download the template, fill it in, upload it. Columns: Agent Name, Employee ID, Current Manager, Region, Department, Work Days, Rest Days, Shift. The upload takes Excel (.xlsx, first sheet) or CSV. Work Days and Rest Days each take a range (`Mon-Fri`) or a list (`Tue,Wed,Thu,Fri,Sat`). Fill in either one and the other is worked out; if both are filled, Work Days is used; if both are blank, Mon-Fri is assumed. Shift is the start and end time in Manila time (PHT, UTC+8), for example `09:00-18:00`, whatever the app timezone is set to; overnight shifts such as `21:00-06:00` are fine. Each agent's Region decides which region's hourly coverage they count toward, so the coverage rows follow whatever roster you upload.
3. Settings > PTO allocation: select a department tile. A window opens with one calendar per region. Type the default slots per day, choose all regions or one, and All months or a month and year, then select Apply. Adjust single days on the calendars. Or choose **Required headcount per day** in the same bar: enter how many agents must stay on duty, and Greenlight works out each day's slots as agents scheduled that day (from the roster's work days) minus that number, never below 0. Days typed by hand still win. **Stop auto** puts the region back on fixed slots, and applying fixed slots for the same months also replaces it. Each day shows S (agents scheduled to work) and HC (headcount left if every PTO slot is taken), which updates as you type. The window shows one region at a time; use the arrows, the region chips or the left and right arrow keys to switch. Above the region's calendar, an hourly coverage table covers the whole month: one row per hour the region actually works, and two columns under each date: Sched (agents scheduled that hour) and After (agents left if every PTO slot that day is taken).
4. Settings > Approval rules: choose Automatic or Manual review, and the minimum notice.

## How a request is decided

For each date, in this order:

1. The person already has an approved or pending request for that date: skipped.
2. Manual review is on: saved as pending. It shows in the admin **Pending** tab, sorted by date, in the "Short notice" column if it was sent later than the minimum notice.
3. Automatic, and the date is sooner than the minimum notice: rejected.
4. Automatic: approved if approved requests for that department, region and day are below the allocation. Otherwise it is recorded as rejected, so an admin can still override it.

Allocation for a day is the day's own number if one was set; else, if a required headcount is set, the agents scheduled that day minus the required headcount (never below 0); else that month's default, else the every-month default, else 0.
An admin override approves a rejected request even when the day is full, and stores the reason.

Override requests: when a picked date is inside the notice window or has no slot left, the agent can select **Request override** and give a reason. In automatic mode the date is still rejected right away, but it is recorded with the reason. Every open override request goes to the admin **Override requests** tab, split into **Short notice** and **No allocation**, filtered by month and year. Admins can approve or reject one, select several and decide them together, or reject all shown. Approving asks for one reason, which is saved on each request.

## PTO report

**Extract report** on the dashboard downloads an Excel file for the month, department and region chosen at the top of the dashboard. It is built in the browser, so it needs no extra server setup. Tabs:

- **Dashboard**: totals, busiest day, status and region tables, and three charts (approved and rejected per day, requests by status, requests by region)
- **Requests**: one row per request with filters and a frozen header. Admins also get the override request columns.
- **Daily**: slots, approved, rejected and pending for each day, with formulas for slots left and totals
- **By team**: each department and region, with slots used as a percentage

## Data in MongoDB

- `roster`: name, employee_id, manager, region, department, work_days, shift
- `requests`: one document per person per date, with status, reason, short_notice, decided_by, override_reason (admin), override_request (agent's reason), override_status (`open`, `approved`, `declined`), override_type (`notice` or `allocation`), note, submitted_at
- `allocations`: `kind: "default"` (key `*` or `YYYY-MM`, slots per day), `kind: "required"` (key `*` or `YYYY-MM`, agents needed on duty, stored in `slots`) and `kind: "day"` (key `YYYY-MM-DD`), each with department and region
- `settings`: one document, `_id: "config"`, with approval_mode and min_notice_days

## Good to know

- Roster shift times are always entered in Manila time (UTC+8). Settings > Timezone sets the app timezone (Manila by default). Greenlight converts shift times to it for the hourly coverage table, schedules, the roster table and reports, and uses it for "today", the minimum notice and submitted times. Daylight saving is handled for each date, so a London or New York table moves by an hour when the clocks change. Changing the timezone never edits the stored roster.

- The calendar refreshes every 12 seconds while the tab is open, so approvals made by other people show up without a reload. Change `POLL_MS` in `index.html` to adjust.
- The admin passcode is one shared secret. Admin actions are checked on the server, not only hidden in the page.
- `DEV_PASSWORD` in secrets is a developer key for maintenance. It always signs in with full admin access, even after an admin changes the passcode, and the header shows **Developer**. It also works as the current passcode in Settings > Admin passcode, so you can reset a forgotten one. It is never stored in MongoDB.
- `ADMIN_PASSWORD` in secrets is the first passcode. Once it is changed in Settings, the new one is stored hashed in MongoDB (`settings.config.admin_password`) and the secret is no longer used. Forgot it? Delete that field in MongoDB and the secret works again.
- Excel upload uses the SheetJS library loaded from cdnjs, so the browser needs internet access for it. CSV upload works without it.
- Requests older than 400 days stay in MongoDB but are not loaded into the app.
