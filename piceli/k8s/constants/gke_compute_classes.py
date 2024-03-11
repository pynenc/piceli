from dataclasses import dataclass


@dataclass(frozen=True, unsafe_hash=True)
class ComputeClassLimits:
    """Compute class limits"""

    compute_class: str
    min_cpu: str
    max_cpu: str
    min_memory: str
    max_memory: str
    min_cpu_memory_ratio: float  # minimum x time more memory than cpu
    max_cpu_memory_ratio: float  # maximum x time more memory than cpu
    min_ephemeral_storage: str = "10Mi"
    max_ephemeral_storage: str = "10Gi"


class ComputeClasses:
    """Compute classes limits"""

    GENERAL_PURPOSE = ComputeClassLimits(
        compute_class="General-purpose",
        min_cpu="0.25",
        max_cpu="30",
        min_memory="0.5Gi",
        max_memory="110Gi",
        min_cpu_memory_ratio=1,
        max_cpu_memory_ratio=6.5,
    )
    BALANCED = ComputeClassLimits(
        compute_class="Balanced",
        min_cpu="0.25",
        max_cpu="222",
        min_memory="0.5Gi",
        max_memory="851Gi",
        min_cpu_memory_ratio=1,
        max_cpu_memory_ratio=8,
    )
    SCALE_OUT = ComputeClassLimits(
        compute_class="Scale-Out",
        min_cpu="0.25",
        max_cpu="43",
        min_memory="1Gi",
        max_memory="172i",
        min_cpu_memory_ratio=4,
        max_cpu_memory_ratio=4,
    )
