# Synthetic test data only

**Never put real patient, referrer, or clinic operational data in this folder.**

All records under `data/testdata/` are invented for automated tests.
They must remain deterministic (fixed IDs and dates) and free of production secrets.

Reference "today" for this seed set: **2026-09-07** (UTC calendar date).
"Tomorrow" appointments use **2026-09-08**.

Load via:

```python
from app.nookal_client.seed import load_clinic_seed, seeded_mock_client

client = seeded_mock_client()
# or
load_clinic_seed(existing_mock_client)
```
