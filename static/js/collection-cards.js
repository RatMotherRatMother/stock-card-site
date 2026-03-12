/**
 * collection-cards.js
 * -------------------------------------------------------------------
 * Manages the stock-symbol card carousel on the Collection page.
 *
 * Public API (called from the inline <script> in collection.html):
 *
 *   CollectionCards.render(symbols, onCardClick)
 *     Shows the card-pack image.  Clicking the pack triggers the
 *     unpack animation, after which cards land in their row and
 *     arrow scrolling is enabled.
 *
 *   CollectionCards.setActive(symbol)
 *     Marks the card for `symbol` as active (gold border / glow).
 *     Called by onSymbolClick() after a card is clicked.
 *
 *   CollectionCards.clear()
 *     Resets all state and empties #symbol-list.
 * -------------------------------------------------------------------
 */

const CollectionCards = (() => {

    // ── Constants ────────────────────────────────────────────────────
    const MAX_VISIBLE    = 6;
    const CARD_W         = 180;   // px — must match CSS / image
    const CARD_GAP       = 12;    // px — gap between cards in the row
    const ANIM_DURATION  = 300;   // ms per card slide
    const ANIM_STAGGER   = 100;   // ms between card starts
    const PACK_FADE_MS   = 180;   // ms for pack fade-out

    // ── Stage → image mapping ─────────────────────────────────────────
    // Keys match the stage values returned by /api/symbols ("0.5", "1", etc.)
    // Fall back to the generic images for any unmapped stage (e.g. "graveyard").
    const STAGE_IMAGES = {
        "graveyard": { card: "/static/images/0.5-card.png", pack: "/static/images/0.5-card-pack.png" },
        "0.5": { card: "/static/images/0.5-card.png", pack: "/static/images/0.5-card-pack.png" },
        "1":   { card: "/static/images/1-card.png",   pack: "/static/images/1-card-pack.png"   },
        "2":   { card: "/static/images/2-card.png",   pack: "/static/images/2-card-pack.png"   },
        "3":   { card: "/static/images/3-card.png",   pack: "/static/images/3-card-pack.png"   },
    };
    const FALLBACK_CARD = "/static/images/card-180x240.png";
    const FALLBACK_PACK = "/static/images/card-pack-.05.png";

    // ── Sound ────────────────────────────────────────────────────────
    // Audio objects are created once and reused.  Creation is deferred
    // to first use (_loadSounds) so the module loads safely even if the
    // sound files are missing or the browser blocks autoplay.
    let _sndTear   = null;   // plays when the pack is clicked
    let _sndCard   = null;   // plays as each card lands

    function _loadSounds() {
        if (_sndTear) return;   // already initialised
        try {
            _sndTear = new Audio("/static/sounds/pack-tear.mp3");
            _sndCard = new Audio("/static/sounds/card-unpack.ogg");
            _sndTear.volume = 0.25;   // 25% quieter than default
            _sndCard.volume = 0.15;   // 50% quieter than default
            // Pre-load into memory so first playback has no delay
            _sndTear.load();
            _sndCard.load();
        } catch (e) {
            // Non-fatal — animation continues without sound
            _sndTear = null;
            _sndCard = null;
        }
    }

    // _playSound(audio)
    //   Rewinds to the start and fires play(), silently swallowing any
    //   NotAllowedError (browser autoplay policy) or missing-file error.
    //   Cloning the node lets the same sound overlap itself (e.g. rapid
    //   card arrivals) without waiting for the previous instance to end.
    function _playSound(audio) {
        if (!audio) return;
        try {
            const instance = audio.cloneNode();   // lightweight clone
            instance.volume = audio.volume;
            instance.play().catch(() => {});       // ignore rejection silently
        } catch (e) { /* non-fatal */ }
    }

    // ── Internal state ───────────────────────────────────────────────
    let _symbols      = [];
    let _startIndex   = 0;
    let _onCardClick  = null;
    let _container    = null;   // #symbol-list
    let _animating    = false;  // true while unpack is in flight
    let _cardImgSrc   = FALLBACK_CARD;   // resolved per stage on render()
    let _packImgSrc   = FALLBACK_PACK;   // resolved per stage on render()

    // ── DOM references ───────────────────────────────────────────────
    let _unpackWrap = null;   // .card-unpack-container
    let _packImg    = null;   // #card-pack
    let _carousel   = null;   // .card-carousel (arrow + row wrapper)
    let _cardRow    = null;   // .card-row
    let _btnLeft    = null;
    let _btnRight   = null;

    // ════════════════════════════════════════════════════════════════
    // PUBLIC: render(symbols, onCardClick, stage)
    //   Called by loadSymbols() in collection.html.
    //   `stage` is the string key ("0.5", "1", "2", "3") used to look
    //   up the matching card and pack images for this stage.
    //   Shows the card pack; cards appear after the user clicks it.
    // ════════════════════════════════════════════════════════════════
    function render(symbols, onCardClick, stage) {
        _symbols     = symbols || [];
        _startIndex  = 0;
        _onCardClick = onCardClick;
        _animating   = false;
        _container   = document.getElementById("symbol-list");

        // Resolve stage-specific images (fall back to generics if unknown)
        const imgs   = STAGE_IMAGES[String(stage)] || {};
        _cardImgSrc  = imgs.card || FALLBACK_CARD;
        _packImgSrc  = imgs.pack || FALLBACK_PACK;

        if (!_container) return;
        _container.innerHTML = "";

        if (_symbols.length === 0) {
            _container.innerHTML = '<span id="symbol-status">No symbols in this stage.</span>';
            return;
        }

        _buildShell();
        _showPack();
    }

    // ════════════════════════════════════════════════════════════════
    // _buildShell()
    //   Creates the permanent DOM structure once per render():
    //     .card-unpack-container
    //       ├── #card-pack  (pack image)
    //       └── .card-carousel
    //             ├── arrow-left
    //             ├── .card-row
    //             └── arrow-right
    // ════════════════════════════════════════════════════════════════
    function _buildShell() {
        // Outer wrapper
        _unpackWrap = document.createElement("div");
        _unpackWrap.className = "card-unpack-container";

        // Pack image
        _packImg = document.createElement("img");
        _packImg.id        = "card-pack";
        _packImg.src       = _packImgSrc;
        _packImg.alt       = "Card pack";
        _packImg.className = "card-pack";
        _packImg.addEventListener("click", _onPackClick);

        // Carousel shell (hidden until animation completes)
        _carousel = document.createElement("div");
        _carousel.className = "card-carousel card-carousel--hidden";

        _btnLeft  = _makeArrow("‹", "left",  () => _scroll(-1));
        _cardRow  = document.createElement("div");
        _cardRow.className = "card-row";
        _btnRight = _makeArrow("›", "right", () => _scroll(+1));

        _carousel.appendChild(_btnLeft);
        _carousel.appendChild(_cardRow);
        _carousel.appendChild(_btnRight);

        _unpackWrap.appendChild(_packImg);
        _unpackWrap.appendChild(_carousel);
        _container.appendChild(_unpackWrap);
    }

    // ════════════════════════════════════════════════════════════════
    // _showPack()
    //   Makes the pack visible and ready to click; hides the carousel.
    // ════════════════════════════════════════════════════════════════
    function _showPack() {
        _packImg.style.opacity    = "1";
        _packImg.style.display    = "block";
        _packImg.style.cursor     = "pointer";
        _carousel.classList.add("card-carousel--hidden");
    }

    // ════════════════════════════════════════════════════════════════
    // _onPackClick()
    //   Tear animation sequence:
    //   1. Quick shake/squish on the whole pack (CSS keyframes).
    //   2. Replace pack image with two half-clones (clip-path).
    //   3. Halves fly apart + fade — simulates ripping the pack open.
    //   4. Build card elements (invisible) at the pack origin.
    //   5. Stagger cards into final flex positions.
    //   6. Clean up absolute layout → normal flex.
    // ════════════════════════════════════════════════════════════════
    function _onPackClick() {
        if (_animating) return;
        _animating = true;

        // Ensure Audio objects exist (no-op after first call)
        _loadSounds();

        // ── Play pack-tear sound immediately on click ─────────────────
        _playSound(_sndTear);

        // ── Timing constants (ms) ────────────────────────────────────
        const SHAKE_MS      = 160;   // CSS shake keyframe duration
        const TEAR_MS       = 200;   // halves fly apart
        const CARD_FADE_MS  = 180;   // each card fade-in duration (opacity)
        const CARD_SLIDE_MS = 280;   // each card slide to final position
        const CARD_STAGGER  = 65;    // ms between successive card starts

        // ── Step 1: shake the pack ───────────────────────────────────
        _packImg.classList.add("pack-shake");

        setTimeout(() => {
            // ── Step 2: swap pack image for two clipped halves ───────
            // The halves are absolutely positioned <img> elements that
            // sit exactly on top of the original pack, then animate
            // apart.  We read the pack rect BEFORE hiding it.
            const packRect = _packImg.getBoundingClientRect();
            const wrapRect = _unpackWrap.getBoundingClientRect();
            const packOffX = packRect.left - wrapRect.left;   // relative to container
            const packOffY = packRect.top  - wrapRect.top;

            // Hide the original (keep in DOM for measurements later)
            _packImg.style.visibility = "hidden";
            _packImg.style.pointerEvents = "none";

            // Top strip  — top 20% of the pack, tears upward
            const halfLeft  = _makeTearPiece("top",    packOffX, packOffY);
            // Body        — bottom 80% of the pack, drops down
            const halfRight = _makeTearPiece("bottom", packOffX, packOffY);

            _unpackWrap.appendChild(halfLeft);
            _unpackWrap.appendChild(halfRight);

            // ── Step 3: trigger tear — strip flies up, body drops ────
            // Double-rAF so browser has painted both pieces first.
            requestAnimationFrame(() => requestAnimationFrame(() => {
                halfLeft.classList.add("pack-half--tearing-top");
                halfRight.classList.add("pack-half--tearing-body");
            }));

            // ── Step 4: build cards while tear is still playing ──────
            // We overlap the card build with the tail end of the tear
            // so the first card appears just as the halves vanish.
            const CARDS_START = TEAR_MS * 0.55;   // ~110ms into the tear

            setTimeout(() => {
                const slice = _symbols.slice(_startIndex, _startIndex + MAX_VISIBLE);
                _cardRow.innerHTML = "";

                // Size the card-row container so it doesn't collapse
                _cardRow.style.position = "relative";
                _cardRow.style.height   = "240px";
                _cardRow.style.width    = `${slice.length * (CARD_W + CARD_GAP) - CARD_GAP}px`;

                // Reveal carousel shell (arrows hidden until cleanup)
                _carousel.classList.remove("card-carousel--hidden");

                // card start offset: where the pack centre was, relative
                // to the card-row's top-left after carousel is shown.
                const rowRect  = _cardRow.getBoundingClientRect();
                const startX   = packRect.left - rowRect.left;
                const startY   = packRect.top  - rowRect.top;

                const cardEls = slice.map((sym, i) => {
                    const card = _makeCardEl(sym);
                    card.style.position  = "absolute";
                    card.style.left      = `${startX}px`;
                    card.style.top       = `${startY}px`;
                    card.style.opacity   = "0";
                    card.style.transform = "translateY(12px) scale(0.88)";
                    card.style.transition = "none";
                    card.style.zIndex    = String(MAX_VISIBLE - i);
                    _cardRow.appendChild(card);
                    return { el: card, sym };
                });

                // ── Step 5: stagger cards to final positions ──────────
                cardEls.forEach(({ el }, i) => {
                    const finalX = i * (CARD_W + CARD_GAP);

                    setTimeout(() => {
                        el.style.transition =
                            `left    ${CARD_SLIDE_MS}ms cubic-bezier(0.18,0.67,0.32,1.0), ` +
                            `top     ${CARD_SLIDE_MS}ms cubic-bezier(0.18,0.67,0.32,1.0), ` +
                            `opacity ${CARD_FADE_MS}ms  ease, ` +
                            `transform ${CARD_SLIDE_MS}ms cubic-bezier(0.18,0.67,0.32,1.0)`;

                        requestAnimationFrame(() => requestAnimationFrame(() => {
                            el.style.left      = `${finalX}px`;
                            el.style.top       = "0px";
                            el.style.opacity   = "1";
                            el.style.transform = "translateY(0) scale(1)";
                        }));

                        // Play card-arrival sound when this card finishes sliding
                        setTimeout(() => _playSound(_sndCard), CARD_SLIDE_MS);
                    }, i * CARD_STAGGER);
                });

                // ── Step 6: clean up after all cards have landed ──────
                const settleDuration =
                    (slice.length - 1) * CARD_STAGGER + CARD_SLIDE_MS + 40;

                setTimeout(() => {
                    // Remove tear piece clones
                    if (halfLeft.parentNode)  halfLeft.parentNode.removeChild(halfLeft);
                    if (halfRight.parentNode) halfRight.parentNode.removeChild(halfRight);
                    // Remove original pack image
                    if (_packImg.parentNode)  _packImg.parentNode.removeChild(_packImg);
                    _packImg = null;

                    // Switch cards from absolute to flex layout
                    cardEls.forEach(({ el }) => {
                        el.style.position  = "";
                        el.style.left      = "";
                        el.style.top       = "";
                        el.style.transition = "";
                        el.style.transform = "";
                        el.style.zIndex    = "";
                    });
                    _cardRow.style.position = "";
                    _cardRow.style.height   = "";
                    _cardRow.style.width    = "";

                    _animating = false;
                    _updateArrows();
                }, settleDuration);

            }, CARDS_START);

        }, SHAKE_MS);
    }

    // ════════════════════════════════════════════════════════════════
    // _makeTearPiece(piece, x, y)
    //   Creates an absolutely-positioned clone of the pack image
    //   clipped to show only the "top" strip (top 20%) or the
    //   "bottom" body (bottom 80%).
    //   `x` and `y` are offsets relative to .card-unpack-container.
    // ════════════════════════════════════════════════════════════════
    function _makeTearPiece(piece, x, y) {
        const el = document.createElement("img");
        el.src       = _packImgSrc;
        el.alt       = "";
        el.className = "pack-half";   // reuse base styles (size, position:absolute)
        el.style.left = `${x}px`;
        el.style.top  = `${y}px`;
        // top strip  → inset(0 0 80% 0)  keeps top 20% of the image
        // body       → inset(20% 0 0 0)  keeps bottom 80% of the image
        el.style.clipPath = piece === "top"
            ? "inset(0 0 80% 0)"
            : "inset(20% 0 0 0)";
        return el;
    }

    // ════════════════════════════════════════════════════════════════
    // _renderCards(skipAnimation?)
    //   Used by _scroll() — re-renders the card row without animation.
    // ════════════════════════════════════════════════════════════════
    function _renderCards() {
        _cardRow.innerHTML = "";
        _cardRow.style.position = "";
        _cardRow.style.height   = "";
        _cardRow.style.width    = "";

        const slice = _symbols.slice(_startIndex, _startIndex + MAX_VISIBLE);
        slice.forEach(sym => _cardRow.appendChild(_makeCardEl(sym)));

        _updateArrows();
    }

    // ════════════════════════════════════════════════════════════════
    // _makeCardEl(sym)
    //   Builds a single .stock-card element with click handler.
    // ════════════════════════════════════════════════════════════════
    function _makeCardEl(sym) {
        const card = document.createElement("div");
        card.className      = "stock-card";
        card.dataset.symbol = sym;

        const img   = document.createElement("img");
        img.src = _cardImgSrc;
        img.alt = sym;

        const label = document.createElement("span");
        label.className   = "card-symbol";
        label.textContent = sym;

        card.appendChild(img);
        card.appendChild(label);
        card.addEventListener("click", () => {
            if (!_animating && _onCardClick) _onCardClick(sym, card);
        });

        return card;
    }

    // ════════════════════════════════════════════════════════════════
    // _scroll(delta)  — arrow navigation; never re-triggers animation
    // ════════════════════════════════════════════════════════════════
    function _scroll(delta) {
        if (_animating) return;
        const maxStart = Math.max(0, _symbols.length - MAX_VISIBLE);
        _startIndex = Math.min(Math.max(0, _startIndex + delta), maxStart);
        _renderCards();
    }

    // ════════════════════════════════════════════════════════════════
    // _updateArrows()
    // ════════════════════════════════════════════════════════════════
    function _updateArrows() {
        const needArrows = _symbols.length > MAX_VISIBLE;
        _btnLeft.style.visibility  = needArrows ? "visible" : "hidden";
        _btnRight.style.visibility = needArrows ? "visible" : "hidden";
        _btnLeft.disabled  = (_startIndex === 0);
        _btnRight.disabled = (_startIndex >= _symbols.length - MAX_VISIBLE);
    }

    // ════════════════════════════════════════════════════════════════
    // _makeArrow(label, side, onClick)
    // ════════════════════════════════════════════════════════════════
    function _makeArrow(label, side, onClick) {
        const btn = document.createElement("button");
        btn.className   = `card-arrow-btn card-arrow-${side}`;
        btn.textContent = label;
        btn.setAttribute("aria-label", side === "left" ? "Scroll left" : "Scroll right");
        btn.addEventListener("click", onClick);
        return btn;
    }

    // ════════════════════════════════════════════════════════════════
    // PUBLIC: setActive(symbol)
    // ════════════════════════════════════════════════════════════════
    function setActive(symbol) {
        if (!_cardRow) return;
        _cardRow.querySelectorAll(".stock-card").forEach(c => {
            c.classList.toggle("active", c.dataset.symbol === symbol);
        });
    }

    // ════════════════════════════════════════════════════════════════
    // PUBLIC: clear()
    // ════════════════════════════════════════════════════════════════
    function clear() {
        _symbols    = [];
        _startIndex = 0;
        _animating  = false;
        _cardImgSrc = FALLBACK_CARD;
        _packImgSrc = FALLBACK_PACK;
        _unpackWrap = null;
        _packImg    = null;
        _carousel   = null;
        _cardRow    = null;
        _btnLeft    = null;
        _btnRight   = null;
        const c = document.getElementById("symbol-list");
        if (c) c.innerHTML = '<span id="symbol-status">Select a stage to see its symbols.</span>';
    }

    // ── Public API ───────────────────────────────────────────────────
    return { render, setActive, clear };

})();