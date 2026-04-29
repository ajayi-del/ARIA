# Tria Bridge UI Assets

This directory holds screenshot templates for OpenCV template matching.

## Required Templates

| Filename | Description | Capture Instructions |
|----------|-------------|---------------------|
| `symbol_search.png` | The search/spotter input field on Tria | Click search bar, capture just the input area |
| `symbol_select.png` | The first dropdown result after typing a symbol | Type "BTC", capture the first result row |
| `buy_button.png` | The green/long/Buy button | Capture the full button at rest (not hovered) |
| `sell_button.png` | The red/short/Sell button | Capture the full button at rest |
| `size_field.png` | The size/notional input field | Capture the input box including label |
| `leverage_dropdown.png` | Leverage selector trigger | Capture the dropdown closed state |
| `confirm_leverage.png` | Button to confirm leverage change | Capture the confirm/apply button |
| `confirm_order.png` | The final "Place Order" or "Confirm" button | Capture at rest, full button |
| `filled_confirmed.png` | The "Order Filled" confirmation toast/overlay | Capture the success state text/background |

## Capture Rules

1. **Resolution lock**: All templates must be captured at the SAME screen resolution the bridge will run at.
2. **Browser zoom**: Set to 100%. Do not change zoom after capture.
3. **Theme**: Capture in the theme you trade with (light/dark). Templates will NOT match across themes.
4. **No hover**: Capture UI elements in their default/rest state, not while hovered.
5. **Tight crop**: Include a small 2-4px margin around the element but avoid excessive background.
6. **PNG format**: Save as 24-bit PNG. Avoid JPEG (compression artifacts break matching).

## Quick Capture Script

```python
# save as capture_template.py, run when you need a new template
import cv2
import numpy as np
import mss

with mss.mss() as sct:
    mon = sct.monitors[1]
    img = np.array(sct.grab(mon))
    cv2.imwrite("template.png", img)
```

Then crop in Preview / GIMP to isolate the element.

## Coordinate Fallbacks (Optional)

If template matching fails (Tria updates UI), you can define hardcoded coordinates
in `tria_bridge/config.py` under `BROWSER_REGION` and use the executor directly.
