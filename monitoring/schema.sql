-- Apply to the dedicated resource_reports database as its administrator.
CREATE TABLE IF NOT EXISTS public.daily_resource_reports (
    report_date date NOT NULL,
    server text NOT NULL,
    report jsonb NOT NULL,
    generated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (report_date, server)
);
REVOKE ALL ON DATABASE resource_reports FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE resource_reports TO resource_report_reader, resource_report_writer;
GRANT USAGE ON SCHEMA public TO resource_report_reader, resource_report_writer;
GRANT SELECT ON public.daily_resource_reports TO resource_report_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.daily_resource_reports TO resource_report_writer;
ALTER ROLE resource_report_reader SET default_transaction_read_only = on;
