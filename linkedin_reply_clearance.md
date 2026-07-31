Appreciate you laying that out. Makes sense — checking density instead of raw points avoids the noise problem, and precomputing a navmesh instead of checking live is the same idea taken further.

On repel vs. flag: I'd start with flag, not repel. A lot of "thin" areas in a point cloud aren't really obstacles, just gaps in scan coverage or thin structures, and pushing the camera away from all of them could steer it away from the exact spots worth seeing. Worth sorting that out before I start building the cost field.
