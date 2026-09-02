# Salt RAAS Migration Tool

A lightweight CLI utility to interactively migrate SaltStack Config / Salt RAAS Jobs and Schedules between servers.

### Prerequisites

* Python 3.8+
* [`uv`](https://github.com/astral-sh/uv) package manager
* User credentials with API access for both source and target RAAS instances

### Setup

1. Place `pyproject.toml` and your script (`migrate.py`) in the same folder.
2. Install dependencies into a managed virtual environment:

```bash
uv sync

```

### Usage

Run the script using `uv`:

```bash
uv run migrate.py

```

### Key Behavior

* **Authentication:** Validates connections to both source and target RAAS API endpoints.
* **Deduplication:** Automatically checks item names and hides jobs or schedules that already exist on the target server.
* **Interactive Selection:** Displays terminal checkboxes to pick exact items for copy.
* **Sanitization & Remapping:**
* Strips Minion Group references to avoid invalid target bindings.
* Re-links copied schedules to newly generated Job UUIDs on the target server.



### Running Without `pyproject.toml`

To execute the script as a standalone file without managing project dependencies:

```bash
uv run --with requests --with questionary migrate.py

```