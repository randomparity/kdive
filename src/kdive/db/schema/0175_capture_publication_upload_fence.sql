-- Restore ADR-0559 capture publication ownership in both upload cleanup paths.
CREATE INDEX capture_operations_publication_owner_key_idx
    ON public.capture_operations (publication_object_key)
    WHERE publication_state IN ('publishing', 'canceling', 'discarded');

-- On-demand cleanup uses the server role; no other journal fields are exposed.
GRANT SELECT (publication_object_key, publication_state)
    ON public.capture_operations TO kdive_server;
