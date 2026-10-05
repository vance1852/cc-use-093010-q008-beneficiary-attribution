"""受益关系与成效归属服务。"""

from .policy import AttributionPolicy, OutcomeTypeRule, PolicyValidationError
from .engine import ENGINE_VERSION, ClusterShare, attribute_cluster, build_claim_views
from .service import AttributionService

__all__ = [
    "AttributionPolicy",
    "OutcomeTypeRule",
    "PolicyValidationError",
    "ENGINE_VERSION",
    "ClusterShare",
    "attribute_cluster",
    "build_claim_views",
    "AttributionService",
]

__version__ = "0.1.0"
