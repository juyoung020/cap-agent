//! A stop signal shared between the program (Ctrl-C) and the running loop.

use std::sync::atomic::{AtomicBool, Ordering};
use tokio::sync::Notify;

#[derive(Default)]
pub struct Cancel {
    flag: AtomicBool,
    notify: Notify,
}

impl Cancel {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn cancel(&self) {
        self.flag.store(true, Ordering::SeqCst);
        self.notify.notify_waiters();
    }

    pub fn is_cancelled(&self) -> bool {
        self.flag.load(Ordering::SeqCst)
    }

    /// Clear the signal so the next turn starts fresh.
    pub fn reset(&self) {
        self.flag.store(false, Ordering::SeqCst);
    }

    /// Resolves once `cancel` has been called.
    pub async fn cancelled(&self) {
        loop {
            let waiting = self.notify.notified();
            tokio::pin!(waiting);
            // Register before the check so a `cancel` between the two is seen.
            waiting.as_mut().enable();
            if self.is_cancelled() {
                return;
            }
            waiting.await;
        }
    }
}

/// `cancelled()` of an optional signal; without one it waits forever.
pub async fn cancelled(cancel: Option<&Cancel>) {
    match cancel {
        Some(c) => c.cancelled().await,
        None => std::future::pending().await,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use std::time::Duration;

    #[tokio::test]
    async fn a_waiting_task_wakes_on_cancel_and_reset_clears_it() {
        let c = Arc::new(Cancel::new());
        let waiter = tokio::spawn({
            let c = c.clone();
            async move { c.cancelled().await }
        });
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(!waiter.is_finished());
        c.cancel();
        tokio::time::timeout(Duration::from_secs(2), waiter).await.unwrap().unwrap();
        // Already cancelled: resolves at once.
        tokio::time::timeout(Duration::from_secs(2), c.cancelled()).await.unwrap();
        c.reset();
        assert!(!c.is_cancelled());
        assert!(tokio::time::timeout(Duration::from_millis(30), cancelled(Some(&c))).await.is_err());
        assert!(tokio::time::timeout(Duration::from_millis(30), cancelled(None)).await.is_err());
    }
}
