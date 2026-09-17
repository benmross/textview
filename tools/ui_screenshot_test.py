#!/usr/bin/env python3
"""Firefox smoke/visual tests for TextView (requires Selenium)."""

from __future__ import annotations

import time
import glob
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions.wheel_input import ScrollOrigin
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support import expected_conditions as expected
from selenium.webdriver.support.ui import WebDriverWait


BASE = "http://127.0.0.1:8787"
OUT = Path("test-screenshots")


def wait(driver, selector: str):
    return WebDriverWait(driver, 15).until(
        expected.presence_of_element_located((By.CSS_SELECTOR, selector))
    )


def assert_no_horizontal_overflow(driver) -> None:
    offenders = driver.execute_script(
        """
        const nodes = [document.documentElement, document.body,
          ...document.querySelectorAll('.sidebar,.conversation-list,.conversation,.conversation-copy,.conversation-preview,.thread,.message-scroll,.gallery')];
        const viewport = document.documentElement.clientWidth;
        return nodes.filter(n => {
          if (n.hidden) return false;
          const r=n.getBoundingClientRect();
          const visible=r.right > 0 && r.left < viewport;
          return visible && r.right > viewport + 1;
        }).map(n => {
          const r=n.getBoundingClientRect();
          return {name: n.className || n.tagName, left: r.left, right: r.right, viewport};
        });
        """
    )
    assert not offenders, offenders


def main() -> None:
    OUT.mkdir(exist_ok=True)
    options = Options()
    options.add_argument("-headless")
    snap_firefox = sorted(glob.glob("/snap/firefox/*/usr/lib/firefox/firefox"))
    if snap_firefox:
        options.binary_location = snap_firefox[-1]
    driver = webdriver.Firefox(options=options)
    try:
        driver.set_window_size(390, 844)
        driver.get(BASE)
        wait(driver, ".conversation")
        assert_no_horizontal_overflow(driver)
        initial = len(driver.find_elements(By.CSS_SELECTOR, ".conversation"))
        driver.execute_script(
            "const n=document.querySelector('.conversation-list');n.scrollTop=n.scrollHeight"
        )
        WebDriverWait(driver, 10).until(
            lambda current: len(current.find_elements(By.CSS_SELECTOR, ".conversation")) > initial
        )
        driver.save_screenshot(str(OUT / "mobile-main.png"))

        search = driver.find_element(By.ID, "searchInput")
        search.send_keys("college")
        wait(driver, ".grouped-result")
        summary = driver.find_element(By.CSS_SELECTOR, ".search-summary").text
        assert "occurrences across" in summary
        assert_no_horizontal_overflow(driver)
        driver.save_screenshot(str(OUT / "mobile-search.png"))

        driver.find_element(By.CSS_SELECTOR, ".grouped-result").click()
        wait(driver, ".message-row.search-target")
        wait(driver, "#matchStatus")
        WebDriverWait(driver, 10).until(
            lambda current: "of" in current.find_element(By.ID, "matchStatus").text
        )
        assert_no_horizontal_overflow(driver)
        driver.save_screenshot(str(OUT / "mobile-search-thread.png"))

        driver.get(f"{BASE}/#conversation=192&message=401323")
        wait(driver, "#message-401323")
        reaction = driver.find_element(By.CSS_SELECTOR, "#message-401323 .reactions").text
        assert "❤" in reaction
        assert not driver.find_elements(By.CSS_SELECTOR, "#message-401326")
        assert_no_horizontal_overflow(driver)
        driver.save_screenshot(str(OUT / "mobile-reaction.png"))

        before = len(driver.find_elements(By.CSS_SELECTOR, ".message-row"))
        driver.execute_script("document.querySelector('.message-scroll').scrollTop=0")
        WebDriverWait(driver, 10).until(
            lambda current: len(current.find_elements(By.CSS_SELECTOR, ".message-row")) > before
        )

        driver.get(f"{BASE}/#conversation=192&message=506563")
        thread_image = wait(driver, '#message-506563 [data-part="585553"] img')
        assert "/api/thumbnail/" in thread_image.get_attribute("src")
        driver.find_element(By.CSS_SELECTOR, '#message-506563 [data-part="585553"]').click()
        wait(driver, "#viewer:not([hidden])")
        viewer_image = wait(driver, "#viewerContent img")
        assert "/api/media/" in viewer_image.get_attribute("src")
        WebDriverWait(driver, 15).until(
            lambda current: " of " in current.find_element(By.ID, "viewerCounter").text
        )
        first_counter = driver.find_element(By.ID, "viewerCounter").text
        driver.find_element(By.ID, "nextAttachment").click()
        assert driver.find_element(By.ID, "viewerCounter").text != first_counter
        assert_no_horizontal_overflow(driver)
        driver.save_screenshot(str(OUT / "mobile-viewer.png"))
        driver.find_element(By.ID, "showInConversation").click()
        target = wait(driver, "#message-506560")
        WebDriverWait(driver, 5).until(
            lambda _current: abs(driver.execute_script(
                """
                const target=arguments[0].getBoundingClientRect();
                const scroll=document.querySelector('.message-scroll').getBoundingClientRect();
                return (target.top + target.height / 2) - (scroll.top + scroll.height / 2);
                """, target
            )) < 80
        )

        # Regression: this portrait JPEG previously expanded Safari's viewer
        # grid beyond the viewport instead of being contained.
        driver.get(f"{BASE}/#conversation=513&message=504120")
        wait(driver, '#message-504120 [data-part="582429"]').click()
        image = wait(driver, "#viewerContent img")
        WebDriverWait(driver, 15).until(
            lambda _current: driver.execute_script(
                "return arguments[0].complete && arguments[0].naturalWidth > 0", image
            )
        )
        bounds = driver.execute_script(
            """
            const image=arguments[0].getBoundingClientRect();
            const canvas=document.querySelector('#viewerContent').getBoundingClientRect();
            return {image:{left:image.left,top:image.top,right:image.right,bottom:image.bottom},
                    canvas:{left:canvas.left,top:canvas.top,right:canvas.right,bottom:canvas.bottom},
                    viewport:{width:innerWidth,height:innerHeight}};
            """, image
        )
        assert bounds["image"]["left"] >= bounds["canvas"]["left"] - 1, bounds
        assert bounds["image"]["right"] <= bounds["canvas"]["right"] + 1, bounds
        assert bounds["image"]["top"] >= bounds["canvas"]["top"] - 1, bounds
        assert bounds["image"]["bottom"] <= bounds["canvas"]["bottom"] + 1, bounds
        driver.save_screenshot(str(OUT / "mobile-poolesvil-viewer.png"))
        driver.find_element(By.ID, "closeViewer").click()

        # Native iMessage semantics: app cards, edit history, standalone
        # stickers, placed stickers, and Firefox-compatible sticker viewing.
        driver.get(f"{BASE}/#conversation=52&message=519921")
        app_card = wait(driver, "#message-519921 .app-message-card")
        assert "Poll" in app_card.text and "butt, boobs" in app_card.text
        driver.get(f"{BASE}/#conversation=52&message=520232")
        history = wait(driver, "#message-520232 .edit-history")
        assert "show history" in history.text
        driver.get(f"{BASE}/#conversation=50&message=522004")
        placed = wait(driver, "#message-522004 .sticker-placed img")
        WebDriverWait(driver, 15).until(
            lambda _current: placed.get_property("complete") and
            placed.get_property("naturalWidth") > 0
        )
        driver.get(f"{BASE}/#conversation=52&message=519903")
        standalone = wait(driver, "#message-519903 .sticker-standalone img")
        assert "/api/sticker/" in standalone.get_attribute("src")
        driver.find_element(By.CSS_SELECTOR, "#message-519903 .sticker-standalone").click()
        viewer_sticker = wait(driver, "#viewerContent img")
        assert "/api/sticker/" in viewer_sticker.get_attribute("src")
        driver.find_element(By.ID, "closeViewer").click()

        driver.get(f"{BASE}/?attachments")
        gallery_image = wait(driver, ".gallery-item img")
        assert "/api/thumbnail/" in gallery_image.get_attribute("src")
        assert_no_horizontal_overflow(driver)
        initial_gallery = len(driver.find_elements(By.CSS_SELECTOR, ".gallery-item"))
        driver.execute_script(
            "const n=document.querySelector('.gallery-scroll');n.scrollTop=n.scrollHeight"
        )
        WebDriverWait(driver, 10).until(
            lambda current: len(current.find_elements(By.CSS_SELECTOR, ".gallery-item"))
            > initial_gallery
        )
        driver.save_screenshot(str(OUT / "mobile-gallery.png"))
        driver.find_element(By.CSS_SELECTOR, ".gallery-item").click()
        wait(driver, "#viewer:not([hidden])")
        WebDriverWait(driver, 15).until(
            lambda current: " of " in current.find_element(By.ID, "viewerCounter").text
        )
        driver.find_element(By.ID, "closeViewer").click()

        driver.set_window_size(1440, 900)
        driver.get(f"{BASE}/#conversation=192&message=401323")
        wait(driver, "#message-401323")
        assert_no_horizontal_overflow(driver)
        message_pane = driver.find_element(By.CSS_SELECTOR, ".message-scroll")
        assert driver.execute_script(
            "return arguments[0].scrollHeight > arguments[0].clientHeight", message_pane
        )
        message_before = driver.execute_script("return arguments[0].scrollTop", message_pane)
        ActionChains(driver).scroll_from_origin(
            ScrollOrigin.from_element(message_pane), 0, 450
        ).perform()
        WebDriverWait(driver, 5).until(
            lambda _current: driver.execute_script(
                "return arguments[0].scrollTop", message_pane
            ) != message_before
        )
        sidebar = driver.find_element(By.CSS_SELECTOR, ".conversation-list")
        side_before = driver.execute_script("return arguments[0].scrollTop", sidebar)
        ActionChains(driver).scroll_from_origin(
            ScrollOrigin.from_element(sidebar), 0, 450
        ).perform()
        WebDriverWait(driver, 5).until(
            lambda _current: driver.execute_script(
                "return arguments[0].scrollTop", sidebar
            ) != side_before
        )
        driver.save_screenshot(str(OUT / "desktop-thread.png"))
    finally:
        driver.quit()
    print("Firefox UI tests passed.")


if __name__ == "__main__":
    main()
