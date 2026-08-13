#!/bin/bash

images=(
    "new-joiners"
    "entitlements"
    "policy"
    "sod-test"
    "identities"
    "peer-affinity"
)

for image in "${images[@]}"; do
  cd $image/api
  SOURCE_IMAGE="${image}-api:latest"
  TARGET_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${image}-api:1.0"

  echo "Building $SOURCE_IMAGE"
  docker build --tag "$SOURCE_IMAGE" . 

  echo "Tagging $SOURCE_IMAGE"
  docker tag "$SOURCE_IMAGE" "$TARGET_IMAGE"

  echo "Pushing $TARGET_IMAGE"
  docker push "$TARGET_IMAGE"
  
  cd ../mcp

  SOURCE_IMAGE="${image}-mcp:latest"
  TARGET_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${image}-mcp:1.0"

  echo "Building $SOURCE_IMAGE"
  docker build --tag "$SOURCE_IMAGE" . 

  echo "Tagging $SOURCE_IMAGE"
  docker tag "$SOURCE_IMAGE" "$TARGET_IMAGE"

  echo "Pushing $TARGET_IMAGE"
  docker push "$TARGET_IMAGE"

  cd ../../

done

echo "Completed!"