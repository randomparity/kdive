-- Keep routine job cleanup from silently cascading through exact build-use fences (#1803).
ALTER TABLE public.investigation_build_uses
    DROP CONSTRAINT investigation_build_uses_job_id_fkey,
    ADD CONSTRAINT investigation_build_uses_job_id_fkey
        FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE RESTRICT;
