# General Conference RSS

This small Flask app serves an RSS feed that updates once per day (08:00 local time) with a General Conference talk from the Open Scripture API.

Quick start

1. Create a virtualenv and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Run the app:

```bash
python main.py
```

3. Visit http://127.0.0.1:5000/feed.xml to fetch the RSS feed.

Notes

- The app builds a talk index from the Open Scripture API and rotates through talks daily.
- The scheduler uses the `TZ` environment variable if set; otherwise UTC is used. To run in your local timezone, set `TZ` before starting.