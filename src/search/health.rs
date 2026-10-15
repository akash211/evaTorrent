//! Health scoring engine for filtering stale and ghost torrents.

pub fn compute_health_score(seeders: u32, leechers: u32, age_days: u32, _size_bytes: u64) -> f64 {
    if seeders == 0 && leechers == 0 {
        return 0.0;
    }
    if seeders <= 1 && leechers == 0 && age_days > 90 {
        return 0.02;
    }

    let base = ((seeders as f64) + 1.0).log10() / 3.0;
    let age_factor = if age_days > 0 {
        1.0 / (1.0 + 0.3 * ((age_days as f64) + 1.0).log10())
    } else {
        1.0
    };
    let leech_bonus = if leechers > 0 && seeders > 0 {
        (((leechers as f64) + 1.0).log10() * 0.05).min(0.15)
    } else {
        0.0
    };

    ((base * age_factor) + leech_bonus).clamp(0.0, 1.0)
}
