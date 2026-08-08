status: DONE

files changed:
- src/oikb/connectors/zendesktickets.py
- tests/test_zendesktickets_connector.py

test commands run and outputs:
1. `pytest tests/test_zendesktickets_connector.py -k "checkpoint or renders_comments" -v`
   Output:
   - `2 failed, 7 deselected in 0.19s`
   - `test_build_manifest_uses_min_datetime_when_checkpoint_missing` failed with `assert [] == ['1001.md']`
   - `test_build_manifest_filters_by_checkpoint_and_renders_comments` failed with `FileNotFoundError: Ticket not found: 1001.md`
2. `pytest tests/test_zendesktickets_connector.py -k "checkpoint or renders_comments" -v`
   Output:
   - `2 passed, 7 deselected in 0.12s`
3. `pytest tests/test_zendesktickets_connector.py -v`
   Output:
   - `1 failed, 8 passed in 0.95s`
   - `test_build_manifest_is_empty` failed with `httpx.HTTPStatusError: Client error '404 Not Found' for url 'https://acme.zendesk.com/api/v2/tickets.json?...'`
4. `pytest tests/test_zendesktickets_connector.py -v`
   Output:
   - `9 passed in 0.22s`
5. `pytest tests/test_zendesktickets_connector.py -v`
   Output:
   - `9 passed in 0.30s`

commits created:
- `1c08d69` — feat: add zendesk ticket pagination and checkpointing

concerns or follow-ups:
- None.

---

fix follow-up:
- files changed:
  - `src/oikb/connectors/zendesktickets.py`
  - `tests/test_zendesktickets_connector.py`
- test command:
  - `pytest tests/test_zendesktickets_connector.py -v`
- test output:
  - `10 passed in 0.24s`
- commit hash:
  - `50cb916` — `fix: advance zendesk checkpoints for filtered tickets`
