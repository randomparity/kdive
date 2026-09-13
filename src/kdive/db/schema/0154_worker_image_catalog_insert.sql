-- Allow image-build workers to publish a new catalog row (#2465).
GRANT INSERT ON TABLE public.image_catalog TO kdive_worker;
