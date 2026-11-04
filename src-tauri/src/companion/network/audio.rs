//! Fixed-size PCM16 network framing and bounded jitter queue.
use std::fmt;

pub const SAMPLES: usize = 320;
pub const PAYLOAD: usize = SAMPLES * 2;
pub const HEADER: usize = 17;
pub const FRAME_SIZE: usize = HEADER + PAYLOAD;
pub const QUEUE_FRAMES: usize = 50;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Error {
    Size,
    PayloadTooLarge,
    Magic,
    Version,
    SampleRate,
    Channels,
    OutsideTurn,
    Stream,
    Sequence,
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "audio protocol error: {self:?}")
    }
}
impl std::error::Error for Error {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub flags: u8,
    pub stream_id: u32,
    pub sequence: u32,
    pub samples: [i16; SAMPLES],
}
impl Frame {
    pub fn parse(bytes: &[u8]) -> Result<Self, Error> {
        if bytes.len() > HEADER + 1500 {
            return Err(Error::PayloadTooLarge);
        }
        if bytes.len() < HEADER {
            return Err(Error::Size);
        }
        if &bytes[..4] != b"MRMA" {
            return Err(Error::Magic);
        }
        if bytes[4] != 1 {
            return Err(Error::Version);
        }
        if u16::from_le_bytes([bytes[14], bytes[15]]) != 16_000 {
            return Err(Error::SampleRate);
        }
        if bytes[16] != 1 {
            return Err(Error::Channels);
        }
        if bytes.len() != FRAME_SIZE {
            return Err(Error::Size);
        }
        let mut samples = [0; SAMPLES];
        for (sample, raw) in samples.iter_mut().zip(bytes[HEADER..].chunks_exact(2)) {
            *sample = i16::from_le_bytes([raw[0], raw[1]]);
        }
        Ok(Self {
            flags: bytes[5],
            stream_id: u32::from_le_bytes(bytes[6..10].try_into().unwrap()),
            sequence: u32::from_le_bytes(bytes[10..14].try_into().unwrap()),
            samples,
        })
    }
    pub fn encode(&self) -> [u8; FRAME_SIZE] {
        let mut out = [0; FRAME_SIZE];
        out[..4].copy_from_slice(b"MRMA");
        out[4] = 1;
        out[5] = self.flags;
        out[6..10].copy_from_slice(&self.stream_id.to_le_bytes());
        out[10..14].copy_from_slice(&self.sequence.to_le_bytes());
        out[14..16].copy_from_slice(&16_000u16.to_le_bytes());
        out[16] = 1;
        for (raw, sample) in out[HEADER..].chunks_exact_mut(2).zip(self.samples) {
            raw.copy_from_slice(&sample.to_le_bytes());
        }
        out
    }
}
pub fn parse(bytes: &[u8]) -> Result<Frame, Error> {
    Frame::parse(bytes)
}
pub fn encode(frame: &Frame) -> [u8; FRAME_SIZE] {
    frame.encode()
}

pub struct Ring {
    slots: [Option<Frame>; QUEUE_FRAMES],
    head: usize,
    len: usize,
    dropped: u64,
}
impl Default for Ring {
    fn default() -> Self {
        Self {
            slots: std::array::from_fn(|_| None),
            head: 0,
            len: 0,
            dropped: 0,
        }
    }
}
impl Ring {
    pub fn push(&mut self, frame: Frame) {
        if self.len == QUEUE_FRAMES {
            self.slots[self.head] = None;
            self.head = (self.head + 1) % QUEUE_FRAMES;
            self.len -= 1;
            self.dropped += 1;
        }
        let tail = (self.head + self.len) % QUEUE_FRAMES;
        self.slots[tail] = Some(frame);
        self.len += 1;
    }
    pub fn pop(&mut self) -> Option<Frame> {
        if self.len == 0 {
            return None;
        }
        let frame = self.slots[self.head].take();
        self.head = (self.head + 1) % QUEUE_FRAMES;
        self.len -= 1;
        frame
    }
    pub fn clear(&mut self) {
        while self.pop().is_some() {}
    }
    pub fn dropped(&self) -> u64 {
        self.dropped
    }
}

#[derive(Default)]
pub struct Gate {
    active: Option<(u32, u32)>,
}
impl Gate {
    pub fn start(&mut self, id: u32) {
        self.active = Some((id, 0));
    }
    pub fn stop(&mut self) {
        self.active = None;
    }
    pub fn active(&self) -> Option<u32> {
        self.active.map(|(id, _)| id)
    }
    pub fn accept(&mut self, frame: &Frame) -> Result<(), Error> {
        let (id, next) = self.active.as_mut().ok_or(Error::OutsideTurn)?;
        if frame.stream_id != *id {
            return Err(Error::Stream);
        }
        if frame.sequence != *next {
            return Err(Error::Sequence);
        }
        *next = next.wrapping_add(1);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn frame(n: u32) -> Frame {
        Frame {
            flags: 0,
            stream_id: 7,
            sequence: n,
            samples: [n as i16; SAMPLES],
        }
    }
    #[test]
    fn round_trip_and_rejections() {
        let bytes = frame(2).encode();
        assert_eq!(Frame::parse(&bytes), Ok(frame(2)));
        let mut bad = bytes;
        bad[0] = 0;
        assert_eq!(Frame::parse(&bad), Err(Error::Magic));
        bad = bytes;
        bad[4] = 2;
        assert_eq!(Frame::parse(&bad), Err(Error::Version));
        bad = bytes;
        bad[14] = 0;
        assert_eq!(Frame::parse(&bad), Err(Error::SampleRate));
        bad = bytes;
        bad[16] = 2;
        assert_eq!(Frame::parse(&bad), Err(Error::Channels));
        assert_eq!(Frame::parse(&bytes[..FRAME_SIZE - 1]), Err(Error::Size));
        assert_eq!(Frame::parse(&bytes[..10]), Err(Error::Size));
        assert_eq!(
            Frame::parse(&[bytes.as_slice(), &[0]].concat()),
            Err(Error::Size)
        );
        assert_eq!(
            Frame::parse(&[bytes.as_slice(), &[0; 1501]].concat()),
            Err(Error::PayloadTooLarge)
        );
    }
    #[test]
    fn drop_oldest_and_gate() {
        let mut ring = Ring::default();
        for n in 0..52 {
            ring.push(frame(n));
        }
        assert_eq!(ring.dropped(), 2);
        for n in 2..52 {
            assert_eq!(ring.pop(), Some(frame(n)));
        }
        assert_eq!(ring.pop(), None);
        let mut gate = Gate::default();
        assert_eq!(gate.accept(&frame(0)), Err(Error::OutsideTurn));
        gate.start(7);
        let mut wrong = frame(0);
        wrong.stream_id = 8;
        assert_eq!(gate.accept(&wrong), Err(Error::Stream));
        assert_eq!(gate.accept(&frame(0)), Ok(()));
        assert_eq!(gate.accept(&frame(0)), Err(Error::Sequence));
        gate.stop();
        assert_eq!(gate.accept(&frame(1)), Err(Error::OutsideTurn));
    }
}
