# A validation run can be deleted

<!-- entry 211 -->

A validation run can be DELETED — `Store.delete_feasibility_run` + `DELETE
/api/training/feasibility/runs/{id}`, a Delete button per row. Only `prune_feasibility_
reports` existed, which frees the report DIRS but keeps every metrics row forever; this
discards one run entirely. No in-use guard is needed (unlike a model version): nothing
references a run, since validation scores the labelled data, not a gallery. Same
discipline as the version delete — row in one locked txn, dir after the commit and
outside the lock, and the resolved dir confirmed under `training_root` before the rmtree.
