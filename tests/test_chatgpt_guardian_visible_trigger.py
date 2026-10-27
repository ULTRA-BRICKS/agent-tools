"""Visible model-trigger uniqueness regressions; synthetic browser pages only."""

import pytest
from test_chatgpt_guardian_live import MENU_HTML, run


@pytest.mark.parametrize("position", ["beforebegin", "afterend"])
def test_model_probe_ignores_hidden_duplicate_trigger(position):
    """Responsive hidden controls must not make the sole rendered trigger ambiguous."""

    async def case(page, adapter):
        await page.evaluate(
            """position => {
            const visible = document.querySelector('[data-codex-intelligence-trigger]');
            const hidden = visible.cloneNode(true);
            hidden.style.display = 'none';
            window.hiddenTriggerClicks = 0;
            hidden.onclick = () => window.hiddenTriggerClicks++;
            visible.insertAdjacentElement(position, hidden);
        }""",
            position,
        )
        observed = await adapter.observe()
        assert observed.verified and observed.model == "Astra"
        assert observed.safe_failure()
        assert await page.evaluate("window.hiddenTriggerClicks") == 0
        assert await page.evaluate("window.switches") == 0
        assert not await page.evaluate("Boolean(window.clicks)")
        assert await page.locator("[contenteditable]").text_content() == ""
        assert not (await adapter._menu_state())["open"]

    run(case, html=MENU_HTML)


@pytest.mark.parametrize(
    "visible_count",
    [pytest.param(0, id="none-visible"), pytest.param(2, id="two-visible")],
)
def test_model_probe_refuses_nonunique_visible_trigger(visible_count):
    async def case(page, adapter):
        await page.evaluate(
            """count => {
            const trigger = document.querySelector('[data-codex-intelligence-trigger]');
            const duplicate = trigger.cloneNode(true);
            trigger.insertAdjacentElement('afterend', duplicate);
            window.triggerClicks = 0;
            for (const control of [trigger, duplicate]) {
                if (count === 0) control.style.display = 'none';
                control.addEventListener('click', () => window.triggerClicks++);
            }
        }""",
            visible_count,
        )
        observed = await adapter.observe()
        assert not observed.verified
        assert adapter.probe is None
        assert await page.evaluate("window.triggerClicks") == 0
        assert not await page.evaluate("Boolean(window.clicks)")

    run(case, html=MENU_HTML)


@pytest.mark.parametrize("state", ["draft", "running"])
def test_hidden_duplicate_never_allows_unsafe_model_probe(state):
    async def case(page, adapter):
        await page.evaluate(
            """state => {
            const trigger = document.querySelector('[data-codex-intelligence-trigger]');
            const hidden = trigger.cloneNode(true);
            hidden.style.display = 'none';
            trigger.insertAdjacentElement('beforebegin', hidden);
            window.triggerClicks = 0;
            trigger.addEventListener('click', () => window.triggerClicks++);
            if (state === 'draft') document.querySelector('[contenteditable]').textContent = 'owner draft';
            else document.querySelector('[data-talvt-turn-state]').setAttribute('data-talvt-turn-state', 'in_progress');
        }""",
            state,
        )
        observed = await adapter.observe()
        assert not observed.verified
        assert not observed.safe_failure()
        assert await page.evaluate("window.triggerClicks") == 0
        assert not await page.evaluate("Boolean(window.clicks)")

    run(case, html=MENU_HTML)
