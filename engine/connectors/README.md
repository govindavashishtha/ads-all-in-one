# engine/connectors

Owner: connectors/ingestion agent. Typed wrappers around official SDKs (§4.1). The ONLY place that imports facebook_business / google.ads and the ONLY place that converts money (money.py) and platform ids (§0 rule 4). `ReadConnector`/`WriteConnector` interfaces live in base.py — implement them in meta_read.py, meta_write.py, google_read.py, google_write.py, ga4.py.
