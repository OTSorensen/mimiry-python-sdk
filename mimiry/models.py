"""TypedDict definitions for Mimiry API responses.

These provide IDE autocompletion and type checking without adding
runtime dependencies. All API methods return plain dictionaries
that conform to these shapes.
"""

import sys

if sys.version_info >= (3, 8):
    from typing import TypedDict, List, Optional
else:
    from typing import List, Optional
    from typing_extensions import TypedDict


class Job(TypedDict, total=False):
    id: str
    name: Optional[str]
    status: str
    provider: str
    instance_type: Optional[str]
    image: Optional[str]
    location: Optional[str]
    ssh_key_ids: Optional[List[str]]
    startup_script: Optional[str]
    provider_instance_id: Optional[str]
    error_message: Optional[str]
    output: Optional[str]
    config: Optional[dict]
    heartbeat_timeout_seconds: int
    max_runtime_seconds: Optional[int]
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    last_heartbeat: Optional[str]


class SSHKey(TypedDict, total=False):
    id: str
    name: str
    datacrunch_id: Optional[str]
    created_at: str


class InstanceType(TypedDict, total=False):
    instance_type: str
    description: str
    gpu_type: str
    gpu_count: int
    gpu_memory_gb: float
    cpu_cores: int
    ram_gb: float
    storage_gb: float
    price_per_hour: float
    currency: str
    provider: str


class Location(TypedDict, total=False):
    code: str
    name: str
    country: str
    provider: str


class Availability(TypedDict, total=False):
    instance_type: str
    is_available: bool
    locations: List[str]
    provider: str


class OSImage(TypedDict, total=False):
    code: str
    name: str
    os: str
    version: str
    cuda_version: Optional[str]
    provider: str


class Provider(TypedDict, total=False):
    slug: str
    name: str
    is_active: bool
