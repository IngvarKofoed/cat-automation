# Skip done takes its own row below 480px

<!-- entry 515 -->

Review repair: "Skip done" takes its OWN row below 480px. It is `white-space: nowrap`,
so its automatic minimum width is its whole label and the flex row could take space
from nowhere else — the two nav buttons absorbed it and wrapped to two lines each
(measured 37 -> 59 px tall at 360px). This is also what the spec prescribed for that
breakpoint. Missed by the browser pass, which measured 390px only.
