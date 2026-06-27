I want to make styling-only changes to this app's UI — no logic changes, just CSS.

Currently, the app has a "theme" system, but it only changes colors. I want to restructure this into two separate concepts:

1. Color scheme — rename the existing color-switching feature to "Accent Colors." This keeps doing exactly what it does now (just swapping colors).
2. Theme (new) — a real theme system that changes the actual visual style of UI elements (shapes, spacing, borders, shadows, fonts, etc.), not just colors.

For the new theme system:
• The current CSS/styling becomes the default theme (so nothing breaks for existing users).
• Add a second theme modeled after the visual style of the AniList website — I'll want this to reflect how AniList styles its cards, buttons, lists, and other UI elements.
• Users should be able to pick between these two themes independently of their color scheme choice.

───

Add a setting in the settings page to activate and deactivate the Backdrop in manga list and manga detail (volume detail the same). They can be selected independantly. SWITCHES.

───

Add a third theme option called Custom, alongside Default and AniList.

When the user selects "Custom," open a popup/modal that works like a CSS editor — similar to how Jellyfin lets users write their own custom CSS theme.

Popup behavior:
• The popup displays the CSS for all HTML pages/views in the app, one after another, each separated by a clear comment/divider (e.g. /* ---- Page: Home ---- */) so the user knows which CSS block belongs to which page.
• The content shown should default to the current active theme's CSS (so the user starts editing from a known baseline rather than a blank file).
• The entire CSS in the popup must be fully editable — a plain text/code editor area (textarea or code editor component) the user can freely type into.
• There should be a Save button that takes whatever CSS is in the editor and applies it as the new "Custom" theme, persisting it (same storage mechanism as the other themes — localStorage/DB/settings) + a textfield to give that custom theme a Name (so the user can have more customm themes).
• Once saved, selecting "Custom" applies this saved CSS across the app, the same way Default and AniList themes are applied.
• The user should be able to reopen the popup later and continue editing/updating their custom CSS at any time.
P.S. manga_detail.html and volume_detail.html will share the same CSS since it's basically the same page.

Other considerations:
• Make sure invalid/broken CSS doesn't crash the app — just apply whatever is valid and ignore/skip errors gracefully (like browsers normally do with bad CSS).
• Consider adding a "Reset to Default" or "Reset to [Theme]" button inside the popup, in case the user wants to start over from one of the built-in themes.

───

Finish the Fetch metadata feature.