# Admin retires analyse button only once visit left unanalyzed

<!-- entry 290 -->

A job can succeed and still record nothing for its span (frames evicted mid-run), so
admin only retires the analyse button once the visit actually left `unanalyzed` —
otherwise the retry control vanished under the line "Analysed: not analysed."
