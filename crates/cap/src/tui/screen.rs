//! The terminal the window is drawn on.

use super::text::{BG, BORDER, FG, Line, MUTED, Rgb, Style};
use super::ui::Frame;
use crossterm::{cursor, event, execute, terminal};
use std::io::Write;

/// How many colours the terminal is given.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Colours {
    /// Bold and faint only.
    None,
    /// The 256 colours every colour terminal knows.
    Indexed,
    /// Any colour.
    True,
}

impl Colours {
    pub fn detect(env: &dyn Fn(&str) -> Option<String>) -> Colours {
        if env("NO_COLOR").is_some() {
            return Colours::None;
        }
        let any = cfg!(windows) || env("COLORTERM").is_some_and(|v| v.contains("truecolor") || v.contains("24bit"));
        if any { Colours::True } else { Colours::Indexed }
    }
}

/// The nearest of the 256 colours.
fn indexed(colour: Rgb) -> u8 {
    let Rgb(r, g, b) = colour;
    if r.max(g).max(b) - r.min(g).min(b) < 12 {
        // The ramp of 24 greys: 8, 18, 28 and so on.
        return 232 + (u16::from(g).saturating_sub(3) / 10).min(23) as u8;
    }
    // The cube of 6 × 6 × 6 colours: 0, 95, 135, 175, 215, 255.
    let step = |v: u8| {
        if v < 48 {
            0
        } else if v < 115 {
            1
        } else {
            (v - 35) / 40
        }
    };
    16 + 36 * step(r) + 6 * step(g) + step(b)
}

fn code(style: Style, colours: Colours) -> String {
    let bold = if style.bold { ";1" } else { "" };
    let (fg, bg) = (style.fg.unwrap_or(FG), style.bg.unwrap_or(BG));
    match colours {
        Colours::True => format!("\x1b[0{bold};38;2;{};{};{};48;2;{};{};{}m", fg.0, fg.1, fg.2, bg.0, bg.1, bg.2),
        Colours::Indexed => format!("\x1b[0{bold};38;5;{};48;5;{}m", indexed(fg), indexed(bg)),
        Colours::None => format!("\x1b[0{bold}{}m", if fg == MUTED || fg == BORDER { ";2" } else { "" }),
    }
}

/// A row as the bytes that draw it: the row is emptied first, then its pieces follow.
pub fn encode(line: &Line, colours: Colours) -> String {
    let mut out = code(Style::default(), colours);
    let mut last = out.clone();
    out.push_str("\x1b[K");
    for span in &line.spans {
        let style = Style { bg: span.style.bg.or(line.bg), ..span.style };
        let wanted = code(style, colours);
        if wanted != last {
            out.push_str(&wanted);
            last = wanted;
        }
        out.push_str(&span.text);
    }
    out
}

/// Give the terminal back as it was.
pub fn restore() {
    let mut out = std::io::stdout();
    #[cfg(not(windows))]
    let _ = execute!(out, event::DisableBracketedPaste);
    let _ = execute!(out, event::DisableMouseCapture, terminal::LeaveAlternateScreen, cursor::Show);
    let _ = terminal::disable_raw_mode();
}

/// The terminal while the window owns it.
pub struct Term {
    colours: Colours,
    /// The rows as they were drawn last.
    drawn: Vec<String>,
    size: (u16, u16),
}

impl Term {
    pub fn open(colours: Colours, title: &str) -> std::io::Result<Term> {
        terminal::enable_raw_mode()?;
        let mut out = std::io::stdout();
        let opened = execute!(out, terminal::EnterAlternateScreen, event::EnableMouseCapture, terminal::SetTitle(title));
        #[cfg(not(windows))]
        let opened = opened.and_then(|()| execute!(out, event::EnableBracketedPaste));
        if let Err(error) = opened {
            restore();
            return Err(error);
        }
        // A crash must not leave the terminal in the window's state, nor hide its own message.
        let before = std::panic::take_hook();
        std::panic::set_hook(Box::new(move |info| {
            restore();
            before(info);
        }));
        Ok(Term { colours, drawn: Vec::new(), size: (0, 0) })
    }

    /// Columns and rows. A change of size makes the next drawing a full one.
    pub fn size(&mut self) -> (usize, usize) {
        let size = terminal::size().unwrap_or((80, 24));
        if size != self.size {
            self.size = size;
            self.drawn.clear();
        }
        (usize::from(size.0), usize::from(size.1))
    }

    /// Draw everything again next time.
    pub fn forget(&mut self) {
        self.drawn.clear();
    }

    /// Bring the terminal to the frame, writing only the rows that changed.
    pub fn draw(&mut self, frame: &Frame) -> std::io::Result<()> {
        // The terminal shows the whole change at once where it knows how to.
        let mut out = String::from("\x1b[?2026h\x1b[?25l");
        if self.drawn.len() != frame.rows.len() {
            self.drawn.clear();
            out.push_str(&code(Style::default(), self.colours));
            out.push_str("\x1b[2J");
        }
        for (at, row) in frame.rows.iter().enumerate() {
            let bytes = encode(row, self.colours);
            if self.drawn.get(at) == Some(&bytes) {
                continue;
            }
            out.push_str(&format!("\x1b[{};1H{bytes}", at + 1));
            match self.drawn.get_mut(at) {
                Some(old) => *old = bytes,
                None => self.drawn.push(bytes),
            }
        }
        out.push_str("\x1b[0m");
        if let Some((column, row)) = frame.cursor {
            out.push_str(&format!("\x1b[{};{}H\x1b[?25h", row + 1, column + 1));
        }
        out.push_str("\x1b[?2026l");
        let mut stdout = std::io::stdout().lock();
        stdout.write_all(out.as_bytes())?;
        stdout.flush()
    }
}

impl Drop for Term {
    fn drop(&mut self) {
        restore();
    }
}

#[cfg(test)]
mod tests {
    use super::super::text::{BRAND, SURFACE};
    use super::*;

    #[test]
    fn a_row_is_emptied_and_then_drawn_piece_by_piece() {
        let row = Line::styled("› ", Style::fg(BRAND).bold()).with("hi", Style::fg(FG)).with(" there", Style::fg(FG)).on(SURFACE);
        assert_eq!(
            encode(&row, Colours::True),
            "\x1b[0;38;2;230;230;235;48;2;15;15;18m\x1b[K\
             \x1b[0;1;38;2;138;180;248;48;2;22;22;27m› \
             \x1b[0;38;2;230;230;235;48;2;22;22;27mhi there"
        );
        assert_eq!(encode(&Line::styled("plain", Style::default()), Colours::True), "\x1b[0;38;2;230;230;235;48;2;15;15;18m\x1b[Kplain");
    }

    #[test]
    fn a_terminal_with_fewer_colours_gets_the_nearest_ones() {
        assert_eq!(indexed(Rgb(0, 0, 0)), 232);
        assert_eq!(indexed(BG), 233);
        assert_eq!(indexed(Rgb(255, 255, 255)), 255);
        assert_eq!(indexed(Rgb(255, 0, 0)), 196);
        assert_eq!(indexed(Rgb(95, 135, 255)), 69);
        assert_eq!(
            encode(&Line::styled("x", Style::fg(Rgb(255, 0, 0))), Colours::Indexed),
            "\x1b[0;38;5;254;48;5;233m\x1b[K\x1b[0;38;5;196;48;5;233mx"
        );
    }

    #[test]
    fn without_colours_only_bold_and_faint_are_left() {
        let row = Line::styled("note", Style::fg(MUTED)).with("!", Style::fg(BRAND).bold()).on(SURFACE);
        assert_eq!(encode(&row, Colours::None), "\x1b[0m\x1b[K\x1b[0;2mnote\x1b[0;1m!");
        let set = |name: &str| (name == "NO_COLOR").then(|| "1".to_string());
        assert_eq!(Colours::detect(&set), Colours::None);
        let rich = |name: &str| (name == "COLORTERM").then(|| "truecolor".to_string());
        assert_eq!(Colours::detect(&rich), Colours::True);
    }
}
