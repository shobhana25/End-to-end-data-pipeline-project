"""Health capacity warehouse - an end-to-end data pipeline.

Layers, in the order they run:

    ingest  ->  stage  ->  transform  ->  quality  ->  dashboard

``ingest``     pulls declared sources over HTTP into an immutable landing zone
               and records provenance (URL, checksum, byte count, run id).
``stage``      cleans and conforms each landing file in Python, writing typed
               Parquet.
``transform``  runs the SQL models that build the star schema in DuckDB.
``quality``    runs declarative assertions against the built models.
``dashboard``  renders the reporting marts to a static HTML dashboard.
"""

__version__ = "1.0.0"
