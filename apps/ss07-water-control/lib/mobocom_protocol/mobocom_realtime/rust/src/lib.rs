//! Mobocom V2.2 implementation candidate. ACK is acceptance, not physical completion.
//! Device calls belong to one owner task; driver callbacks must be bounded and nonblocking.
#![cfg_attr(not(feature = "std"), no_std)]
#![forbid(unsafe_code)]

pub mod wire;
#[rustfmt::skip]
pub mod device_types;
pub mod client;
pub mod configuration;
pub mod device;
pub mod model;
pub mod scheduler;
pub mod timecode;
#[cfg(feature = "std")]
pub mod udp;

pub use device::{DeviceCore, Driver, LocalControlInput};
pub use device_types::DeviceType;
pub use model::*;
pub use wire::{Command, ErrorCode, Frame, Kind};
