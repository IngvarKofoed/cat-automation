# Windowed decode uses a deque of futures, not a producer queue

<!-- entry 399 -->

Deliberately NOT the stateless path's producer-thread + queue + sentinel machinery: it
exists because that consumer batches GPU calls and must drain asynchronously, whereas
the windowed consumer is a plain in-order loop, where a bounded deque of futures keeps
order by construction with no anti-wedge protocol to get right.
