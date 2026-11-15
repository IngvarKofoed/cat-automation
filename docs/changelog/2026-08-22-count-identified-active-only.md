# count_identified_crops gained active_only

<!-- entry 191 -->

`count_identified_crops` gained the same `active_only` flag and the build/probe
endpoints + CLI pass it. It is the PRE-CHECK guarding those jobs, so counting retired
cats the job then filters out would wave through a build that returns
`insufficient_labels` — the guard must count exactly what the work embeds.
