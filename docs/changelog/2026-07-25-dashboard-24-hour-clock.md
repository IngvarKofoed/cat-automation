# Dashboard timestamps use a 24-hour clock

<!-- entry 50 -->

All dashboard timestamps now render on a 24-hour clock (`formatTime` forces `hour12:false`)
instead of following the locale's AM/PM default — every readout flows through formatTime, the
lone chokepoint. Explicit date+h:m:s components keep the prior layout (4-digit year); only the
clock changed. Presentation only.
