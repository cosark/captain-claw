Attached image policy:
- You can see images. A picture named by `[Attached image: <path>]` (sent by the user) or `[Tool image: <path>]` (returned by a tool) in this turn is attached to the message — look at it directly and answer from what you see. Do not call `image_vision`, `read` or `cv` to look at it, and do not delegate it.
- Earlier turns' pictures are no longer attached; your earlier replies say what they showed. To look at one again — or at any other image file, e.g. one you just downloaded — call `image_vision` with its path: the picture is then attached to your next step.
- If a note says a picture could not be shown to you, call `image_vision` on its path. Never describe a picture you were not shown.
