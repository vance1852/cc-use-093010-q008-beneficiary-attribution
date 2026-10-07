"""受益关系与成效归属服务。"""

from .contracts import Policy, PolicyError
from .rules import MergeRecord, cluster_claims, split_conserved
from .service import AttributionService

__all__ = [
    "AttributionService",
    "MergeRecord",
    "Policy",
    "PolicyError",
    "cluster_claims",
    "split_conserved",
]

__version__ = "0.1.0"
