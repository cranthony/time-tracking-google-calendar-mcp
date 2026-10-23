//! The eMudhra intermediates whose keys were compromised must not be trusted
//! (https://render.com/docs/advisory-emudhra). ureq verifies with the roots
//! webpki-roots compiles in and rustls-webpki's distrust list, so this checks
//! what the deployed build trusts. It reaches the network, so it's ignored by
//! default: `cargo test --locked --test emudhra_distrust -- --ignored`.

const TEST_HOSTS: [&str; 2] = [
    "https://dv-g3b-emudhra.we-have-a-problem.com/",
    "https://dv-g3b-aaa-emudhra.we-have-a-problem.com/",
];

#[test]
#[ignore = "reaches the network"]
fn compromised_emudhra_chains_fail_verification() {
    for url in TEST_HOSTS {
        match ureq::get(url).call() {
            Err(ureq::Error::Transport(err)) => {
                let message = err.to_string();
                assert!(
                    message.to_lowercase().contains("certificate"),
                    "{url} failed, but not on its certificate: {message}"
                );
            }
            Ok(_) | Err(ureq::Error::Status(..)) => {
                panic!("{url} was trusted; the compromised eMudhra certs still verify")
            }
        }
    }
}
