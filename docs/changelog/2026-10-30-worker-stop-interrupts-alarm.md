# Stopping live-naming worker ends a sounding alarm as interrupted

<!-- entry 536 -->

Stopping the live-naming worker now ends a sounding alarm as `interrupted`, on the
thread's way out. The episode lived only in that worker's memory, so its row read "still
sounding" until the next process restart, and a later start resumed it with a wrong length.
