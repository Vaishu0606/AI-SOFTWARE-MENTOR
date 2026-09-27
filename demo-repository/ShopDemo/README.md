# ShopDemo

A minimal e-commerce application used as the controlled demonstration target for
the AI Software Mentor.

## Stack

- Python 3.11+
- FastAPI
- pytest

## Running

```bash
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8001
```

## Tests

```bash
pytest tests/ -v
```

## Demo Scenario

This application contains an intentional bug in its discount + checkout flow.
The AI Software Mentor uses this repository as its primary demonstration target.
