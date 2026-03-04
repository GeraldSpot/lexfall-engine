"""
LEXFALL DATA ADAPTER
====================
The universal translator between any retailer's database and Lexfall.

This is what makes onboarding easy. The retailer doesn't change anything
on their end. They just tell us what their fields are called, and this
adapter handles the rest.

Supports:
  - REST API (retailer exposes an endpoint)
  - Direct DB (PostgreSQL, MySQL, Firebase, MongoDB)
  - CSV/SFTP (file upload)
"""

import json
import csv
import io
import logging
from datetime import datetime, date
from typing import Optional
from dataclasses import dataclass, asdict

import asyncpg        # PostgreSQL
import httpx          # REST API calls
# import firebase_admin  # Firebase (optional)
# import pymongo         # MongoDB (optional)

logger = logging.getLogger("lexfall.adapter")


# ── Standardized Employee Format ──
# No matter what the retailer calls their fields, everything
# gets normalized into this structure.

@dataclass
class LexfallEmployee:
    employee_id: str
    org_id: str
    external_id: str
    name: str
    job_title: str
    department: str
    store_location: str = ""
    hire_date: Optional[str] = None
    manager_name: str = ""
    manager_notes: str = ""
    preferred_lang: str = "en"

    def to_dict(self):
        return asdict(self)


@dataclass
class LexfallTrainingRecord:
    employee_id: str
    org_id: str
    module_name: str
    date: str
    score: float
    passed: bool


# ── Field Mapping Engine ──
# Each retailer maps their field names to ours ONE TIME during onboarding.
# After that, the adapter handles everything automatically.

class FieldMapper:
    """
    Translates retailer field names to Lexfall field names.
    
    Example: Walmart calls employee ID "WIN" and job title "job_code_desc".
    We store that mapping and auto-translate every sync.
    """

    def __init__(self, mappings: dict, transforms: dict = None):
        """
        mappings: {"lexfall_field": "client_field", ...}
            e.g. {"employee_id": "WIN", "name": "associate_name", ...}
        transforms: {"lexfall_field": ("transform_type", args), ...}
            e.g. {"hire_date": ("date_format", {"from": "MM/DD/YYYY"})}
        """
        self.mappings = mappings
        self.transforms = transforms or {}

    def translate(self, raw_record: dict) -> dict:
        """Convert a single record from client format to Lexfall format."""
        result = {}
        for lexfall_field, client_field in self.mappings.items():
            value = raw_record.get(client_field, "")

            # Apply transforms if any
            if lexfall_field in self.transforms:
                t_type, t_args = self.transforms[lexfall_field]
                value = self._apply_transform(value, t_type, t_args)

            result[lexfall_field] = value

        return result

    def _apply_transform(self, value, t_type: str, t_args: dict):
        if t_type == "date_format":
            # Convert from client's date format to ISO
            try:
                from_fmt = t_args.get("from", "%m/%d/%Y")
                parsed = datetime.strptime(str(value), from_fmt)
                return parsed.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                return None

        elif t_type == "uppercase":
            return str(value).upper() if value else ""

        elif t_type == "lowercase":
            return str(value).lower() if value else ""

        elif t_type == "concat":
            # Combine multiple fields (e.g. first_name + last_name)
            fields = t_args.get("fields", [])
            sep = t_args.get("separator", " ")
            # Value here would be the raw record, handled at translate level
            return value

        return value


# ── Adapter: REST API ──
# For retailers who expose an endpoint for us to call.

class RestAPIAdapter:
    """
    Pulls employee data from a retailer's REST API.
    
    Setup: Client gives us their endpoint URL and auth credentials.
    We call it to get employee data on demand or on a schedule.
    """

    def __init__(self, org_id: str, base_url: str, auth_type: str,
                 auth_credentials: dict, mapper: FieldMapper):
        self.org_id = org_id
        self.base_url = base_url.rstrip("/")
        self.auth_type = auth_type
        self.credentials = auth_credentials
        self.mapper = mapper

    def _get_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.auth_type == "bearer":
            headers["Authorization"] = f"Bearer {self.credentials['token']}"
        elif self.auth_type == "api_key":
            headers[self.credentials.get("header", "X-API-Key")] = self.credentials["key"]
        return headers

    async def get_employee(self, external_id: str) -> LexfallEmployee:
        """Pull a single employee by their ID in the client's system."""
        async with httpx.AsyncClient() as client:
            url = f"{self.base_url}/employees/{external_id}"
            response = await client.get(url, headers=self._get_headers(), timeout=10.0)
            response.raise_for_status()
            raw = response.json()

        translated = self.mapper.translate(raw)
        return LexfallEmployee(
            employee_id=f"{self.org_id}_{translated['employee_id']}",
            org_id=self.org_id,
            external_id=translated["employee_id"],
            name=translated.get("name", ""),
            job_title=translated.get("job_title", ""),
            department=translated.get("department", ""),
            store_location=translated.get("store_location", ""),
            hire_date=translated.get("hire_date"),
            manager_name=translated.get("manager_name", ""),
            manager_notes=translated.get("manager_notes", ""),
        )

    async def sync_all_employees(self) -> list[LexfallEmployee]:
        """Pull all employees. Used for full sync."""
        async with httpx.AsyncClient() as client:
            url = f"{self.base_url}/employees"
            response = await client.get(url, headers=self._get_headers(), timeout=30.0)
            response.raise_for_status()
            records = response.json()

        # Handle paginated responses
        if isinstance(records, dict):
            records = records.get("data", records.get("employees", records.get("results", [])))

        employees = []
        for raw in records:
            translated = self.mapper.translate(raw)
            emp = LexfallEmployee(
                employee_id=f"{self.org_id}_{translated['employee_id']}",
                org_id=self.org_id,
                external_id=translated["employee_id"],
                name=translated.get("name", ""),
                job_title=translated.get("job_title", ""),
                department=translated.get("department", ""),
                store_location=translated.get("store_location", ""),
                hire_date=translated.get("hire_date"),
                manager_name=translated.get("manager_name", ""),
                manager_notes=translated.get("manager_notes", ""),
            )
            employees.append(emp)

        logger.info(f"[{self.org_id}] Synced {len(employees)} employees via REST API")
        return employees


# ── Adapter: CSV/SFTP ──
# For retailers who just want to upload a spreadsheet.

class CSVAdapter:
    """
    Imports employee data from a CSV file.
    
    Setup: Client uploads a CSV to our SFTP server (or via web portal).
    We import it. They can do this manually or automate it nightly.
    """

    def __init__(self, org_id: str, mapper: FieldMapper):
        self.org_id = org_id
        self.mapper = mapper

    def parse_csv(self, csv_content: str) -> list[LexfallEmployee]:
        """Parse a CSV string into standardized employee records."""
        employees = []
        reader = csv.DictReader(io.StringIO(csv_content))

        for row in reader:
            translated = self.mapper.translate(row)
            emp = LexfallEmployee(
                employee_id=f"{self.org_id}_{translated['employee_id']}",
                org_id=self.org_id,
                external_id=translated["employee_id"],
                name=translated.get("name", ""),
                job_title=translated.get("job_title", ""),
                department=translated.get("department", ""),
                store_location=translated.get("store_location", ""),
                hire_date=translated.get("hire_date"),
                manager_name=translated.get("manager_name", ""),
                manager_notes=translated.get("manager_notes", ""),
            )
            employees.append(emp)

        logger.info(f"[{self.org_id}] Parsed {len(employees)} employees from CSV")
        return employees

    def parse_file(self, filepath: str) -> list[LexfallEmployee]:
        """Parse a CSV file from disk (e.g. from SFTP upload)."""
        with open(filepath, "r", encoding="utf-8-sig") as f:
            return self.parse_csv(f.read())


# ── Adapter: Direct Database ──
# For retailers who give us read access to their DB.

class DirectDBAdapter:
    """
    Reads employee data directly from the retailer's database.
    
    Setup: Client gives us read-only credentials to a specific 
    table or view. We query it directly.
    """

    def __init__(self, org_id: str, connection_string: str,
                 table_name: str, mapper: FieldMapper):
        self.org_id = org_id
        self.connection_string = connection_string
        self.table_name = table_name
        self.mapper = mapper

    async def get_employee(self, external_id: str) -> LexfallEmployee:
        """Pull a single employee by their external ID."""
        # Determine which field in their table is the ID
        id_field = self.mapper.mappings.get("employee_id", "id")

        conn = await asyncpg.connect(self.connection_string)
        try:
            row = await conn.fetchrow(
                f"SELECT * FROM {self.table_name} WHERE {id_field} = $1",
                external_id
            )
            if not row:
                raise ValueError(f"Employee {external_id} not found")

            translated = self.mapper.translate(dict(row))
            return LexfallEmployee(
                employee_id=f"{self.org_id}_{translated['employee_id']}",
                org_id=self.org_id,
                external_id=translated["employee_id"],
                name=translated.get("name", ""),
                job_title=translated.get("job_title", ""),
                department=translated.get("department", ""),
                store_location=translated.get("store_location", ""),
                hire_date=translated.get("hire_date"),
                manager_name=translated.get("manager_name", ""),
            )
        finally:
            await conn.close()

    async def sync_all_employees(self) -> list[LexfallEmployee]:
        """Pull all employees from their table."""
        conn = await asyncpg.connect(self.connection_string)
        try:
            rows = await conn.fetch(f"SELECT * FROM {self.table_name}")
            employees = []
            for row in rows:
                translated = self.mapper.translate(dict(row))
                emp = LexfallEmployee(
                    employee_id=f"{self.org_id}_{translated['employee_id']}",
                    org_id=self.org_id,
                    external_id=translated["employee_id"],
                    name=translated.get("name", ""),
                    job_title=translated.get("job_title", ""),
                    department=translated.get("department", ""),
                    store_location=translated.get("store_location", ""),
                    hire_date=translated.get("hire_date"),
                    manager_name=translated.get("manager_name", ""),
                )
                employees.append(emp)

            logger.info(f"[{self.org_id}] Synced {len(employees)} employees via Direct DB")
            return employees
        finally:
            await conn.close()


# ── Universal Adapter Factory ──
# Give it an org_id and it figures out which adapter to use.

class AdapterFactory:
    """
    Creates the right adapter based on how the client chose to integrate.
    Called once during setup, returns the adapter you use for everything.
    """

    @staticmethod
    async def create(org_config: dict, field_mappings: dict,
                     field_transforms: dict = None):
        """
        org_config: Row from the organizations table.
        field_mappings: {"lexfall_field": "client_field", ...}
        field_transforms: {"lexfall_field": ("type", args), ...}
        """
        mapper = FieldMapper(field_mappings, field_transforms)
        integration_type = org_config["integration_type"]

        if integration_type == "rest_api":
            return RestAPIAdapter(
                org_id=org_config["org_id"],
                base_url=org_config["api_endpoint"],
                auth_type=org_config["api_auth_type"],
                auth_credentials=json.loads(org_config.get("api_credentials", "{}")),
                mapper=mapper,
            )

        elif integration_type == "direct_db":
            return DirectDBAdapter(
                org_id=org_config["org_id"],
                connection_string=org_config["db_connection"],
                table_name=org_config.get("db_table", "employees"),
                mapper=mapper,
            )

        elif integration_type == "csv":
            return CSVAdapter(
                org_id=org_config["org_id"],
                mapper=mapper,
            )

        else:
            raise ValueError(f"Unknown integration type: {integration_type}")


# ── Example: How Walmart would be configured ──

WALMART_FIELD_MAPPINGS = {
    "employee_id":    "WIN",                # Walmart Identification Number
    "name":           "associate_name",
    "job_title":      "job_code_desc",
    "department":     "dept_name",
    "store_location": "facility_nbr",
    "hire_date":      "original_hire_dt",
    "manager_name":   "direct_supervisor",
}

WALMART_TRANSFORMS = {
    "hire_date": ("date_format", {"from": "%m/%d/%Y"}),
}

# During onboarding, this gets stored in the field_mappings table.
# After that, every sync "just works."
