"""Data Integration (DI): connect manufacturing data sources without code.

    secrets.py    pluggable vault (encrypted local store now; Azure Key Vault adapter later)
    canonical.py  canonical model = the existing sp.* tables the KPI layer reads, with business keys and lineage
    base.py       connector interface every connector implements (test / discover / preview / extract / health)
    registry.py   connector catalogue (one class per connector; the gallery reads this)
    service.py    connections, environments, four-eyes approval and audit log
Integration only supplies data; it never changes KPI definitions or calculations.
"""
