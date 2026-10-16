# Owner operating facts — standing, read before asking the owner (Claude, from the owner's own statements)

Facts the owner has stated about how he operates the account. Treat them as settled; do not ask him again.

- **Manual sales cancel all open orders (owner, 2026-10-16).** When he sells a position by hand in the Alpaca app, he
  always chooses "cancel all open orders" for that symbol. After one of his manual sales, no bot stop or other order is
  left resting on that symbol, so do not ask whether a leftover stop needs cancelling. (A sale by anything else, e.g.
  an unexplained sell, can still leave orders behind; the code must still handle that case.)
