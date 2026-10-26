"""Academic AI Planning Platform — backend package.

Layering (ADR-0001). Dependencies point inward only:

    api -> application -> domain
    infrastructure -> domain
    optimization -> domain
    ai -> domain

`domain` is framework-free: it must never import FastAPI, SQLAlchemy, or
any other framework. This is what keeps the optimizer runnable outside the
web process (local worker, remote worker, HPC/Slurm, Kubernetes).
"""

__version__ = "0.1.0"
